"""Measure holes in the outstanding flight using captures at both ends of one TCP flow."""
import bisect
import csv
import gzip
from pathlib import Path

Range = tuple[int, int]


def accept(ranges: list[Range], start: int, end: int) -> None:
    pos = bisect.bisect_left(ranges, (start, 0))
    if pos and ranges[pos - 1][1] >= start:
        pos -= 1
    stop = pos
    while stop < len(ranges) and ranges[stop][0] <= end:
        start = min(start, ranges[stop][0])
        end = max(end, ranges[stop][1])
        stop += 1
    ranges[pos:stop] = [(start, end)]


def missing(ranges: list[Range], start: int, end: int) -> list[Range]:
    gaps = []
    for left, right in ranges:
        if left >= end:
            break
        if left > start:
            gaps.append((start, left))
        start = max(start, right)
    if start < end:
        gaps.append((start, end))
    return gaps


def ns(value: str) -> int:
    secs, frac = value.split(".")
    return int(secs)*1_000_000_000 + int(frac.ljust(9, "0"))


def read_flow(path: Path, port: int) -> tuple[int, list[tuple[int, int, int]]]:
    with gzip.open(path, "rt") as io_text:
        rows = csv.DictReader(io_text, delimiter="\t")
        base = None
        packets = []
        for row in rows:
            if row["tcp.srcport"] != str(port) or row["tcp.dstport"] != "44000":
                continue
            seq = int(row["tcp.seq_raw"])
            if int(row["tcp.flags"],16) & 2:
                assert base in (None,seq+1), (base,seq)
                base = seq+1
            if length := int(row["tcp.len"]):
                assert base is not None
                seq = (seq-base) % 2**32
                assert seq<2**31, (seq,base)
                packets.append((ns(row["frame.time_epoch"]), seq, seq+length))
        assert base is not None
        assert packets == sorted(packets,key=lambda p:p[0]), path
        return base, packets


def measure(folder: Path, events: list[dict], port: int) -> dict:
    """Only consecutive real expirations on the data socket bound an interval."""
    events = [e for e in events if e["port"] == port]
    result = {"timeouts": len(events), "intervals": []}
    if not events:
        return result

    src = folder / "plain-cubic-netstack-client.1-packets.tsv.gz"
    dst = folder / "plain-cubic-netstack-server.1-packets.tsv.gz"
    base, tx = read_flow(src, port)
    rx_base, rx = read_flow(dst, port)
    assert base == rx_base
    seen_tx, seen_rx = [], []
    tx_pos = rx_pos = 0
    previous = None

    for event in events:
        t = event["ns"]
        while tx_pos < len(tx) and tx[tx_pos][0] < t:
            accept(seen_tx, *tx[tx_pos][1:])
            tx_pos += 1
        while rx_pos < len(rx) and rx[rx_pos][0] < t:
            accept(seen_rx, *rx[rx_pos][1:])
            rx_pos += 1

        if previous is not None:
            then, first_tx, una, nxt, gaps = previous
            assert then < t
            remaining = [sum(end - start for start, end in missing(seen_rx, a, b)) for a, b in gaps]
            result["intervals"].append({
                "duration_ns": t - then,
                "prior_gaps": len(gaps),
                "closed_gaps": remaining.count(0),
                "new_bytes_in_gaps": sum(b - a for a, b in gaps) - sum(remaining),
                "prior_flight_transmissions": sum(b > una and a < nxt for _, a, b in tx[first_tx:tx_pos]),
            })

        una, nxt = ((event[k] - base) % 2**32 for k in ("una", "nxt"))
        assert 0 <= una <= nxt < 2**31
        assert not missing(seen_tx, una, nxt)
        gaps = missing(seen_rx, una, nxt)
        if previous is None:
            # Time until every outstanding byte from the first timeout has
            # reached the receiver. A transfer may end with a gap still open.
            result["first_flight_missing_bytes"] = sum(b - a for a, b in gaps)
            result["first_flight_completion_ns"] = 0 if not gaps else None
            if gaps:
                later = seen_rx.copy()
                for arrival, a, b in rx[rx_pos:]:
                    accept(later, a, b)
                    if not missing(later, una, nxt):
                        result["first_flight_completion_ns"] = arrival - t
                        break
                result["observed_after_first_timeout_ns"] = max(0, rx[-1][0] - t)
        previous = t, tx_pos, una, nxt, gaps
    return result
