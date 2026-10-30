"""Summarize the retained tcp-rack-ack-competition ZIP (one argument).

A witness needs a single original transmission for each newly reported
range, no earlier higher delivery, and a matching RACK probe between this
ACK and the next. Multiset deficits can count lost packets even when two
capture sites disagree on their order, but cannot identify which duplicate
was lost. The companion reorder_episodes.py resolves specific loss epochs
only when the delivered sequence is an ordered subsequence of transmissions.
"""
import bisect
from collections import Counter
import csv
from decimal import Decimal
import gzip
import io
import json
import sys
import zipfile


def packets(archive, name):
    data = gzip.decompress(archive.read(name)).decode()
    return list(csv.DictReader(io.StringIO(data), delimiter="\t"))


def time_ns(packet):
    return int(Decimal(packet["frame.time_epoch"]) * 1_000_000_000)


def flow(packets, sender, receiver):
    return [
        packet for packet in packets
        if (int(packet["tcp.srcport"]), int(packet["tcp.dstport"])) == (sender, receiver)
    ]


def counts(source, sink):
    fields = ("tcp.srcport", "tcp.dstport", "tcp.seq_raw", "tcp.ack_raw",
              "tcp.len", "tcp.flags", "tcp.options.sack_le", "tcp.options.sack_re")

    def identities(packets):
        return Counter(tuple(packet[key] for key in fields) for packet in packets)

    sent = identities(source)
    arrived = identities(sink)
    return {"sent": sum(sent.values()), "arrived": sum(arrived.values()),
            "unmatched_sent": sum((sent - arrived).values()),
            "unmatched_arrived": sum((arrived - sent).values())}


def witnesses(packets, replies, samples):
    syn = next(p for p in packets if int(p["tcp.flags"], 0) & 2)
    isn = (int(syn["tcp.seq_raw"]) + 1) & 0xffffffff

    def relative(raw):
        return (int(raw) - isn) & 0xffffffff

    sends = sorted((relative(p["tcp.seq_raw"]), int(p["tcp.len"]), time_ns(p))
                   for p in packets if int(p["tcp.len"]))
    largest = max(size for _, size, _ in sends)
    extent = max(start + size for start, size, _ in sends)

    def single_original(lo, hi, before):
        left = bisect.bisect_left(sends, (max(0, lo - largest + 1),))
        right = bisect.bisect_left(sends, (hi,))
        overlap = [(s, s + n) for s, n, t in sends[left:right] if t <= before and lo < s + n]
        return overlap == [(lo, hi)]

    # tshark can display relative or raw SACK edges. Disambiguate using the
    # observed stream size before comparing them with ACK and RACK sequences.
    edges = [int(x) for p in replies for key in ("tcp.options.sack_le", "tcp.options.sack_re")
             for x in p[key].split(",") if x]
    display_relative = all(1 <= n <= extent + 1 for n in edges)
    display_raw = all(relative(n) <= extent for n in edges)
    if not edges:
        return []
    if display_relative == display_raw:
        raise ValueError("cannot distinguish raw and relative SACK coordinates")
    edge = (lambda n: int(n) - 1) if display_relative else relative
    acks = [
        (time_ns(p), relative(p["tcp.ack_raw"]),
         [(edge(a), edge(b)) for a, b in zip(p["tcp.options.sack_le"].split(","),
                                            p["tcp.options.sack_re"].split(",")) if a])
        for p in replies if int(p["tcp.flags"], 0) & 16 and relative(p["tcp.ack_raw"]) <= extent + 1
    ]
    times = [sample["ns"] for sample in samples]
    old_ack = old_high = 0
    result = []
    for index, (time, ack, sacks) in enumerate(acks[:-1]):
        high = max([ack] + [right for _, right in sacks])
        if old_ack == old_high and ack > old_ack and len(sacks) == 1:
            left, right = sacks[0]
            j = bisect.bisect_left(times, time)
            if (ack < left and 0 < j < len(samples) and not samples[j-1]["reord"]
                    and times[j] < acks[index+1][0]
                    and (relative(samples[j]["una"]), relative(samples[j]["fack"])) == (ack, right)
                    and single_original(old_ack, ack, time) and single_original(left, right, time)):
                result.append({"old_ack": old_ack, "ack": ack, "sack": [left, right],
                               "reord": samples[j]["reord"], "after_ack_us": (times[j] - time) / 1000,
                               "before_next_ack_us": (acks[index+1][0] - times[j]) / 1000})
        old_ack = max(old_ack, ack)
        old_high = max(old_high, high)
    return result


def report(archive):
    result = {}
    for case, record in json.loads(archive.read("verdict.json"))["passed"].items():
        arm, phase, test = case.split("/")
        stem = f"{arm}/{phase}-{test}"
        server = packets(archive, stem + "-server.1-packets.tsv.gz")
        result[case] = {}
        for lane, wire in record["wire"].items():
            suffix = "-client.1-packets.tsv.gz" if lane == "netstack" else "-client2.1-packets.tsv.gz"
            client = packets(archive, stem + suffix)
            port, peer = wire["client_port"], wire["wan_port"]
            tx = flow(client, port, peer)
            rx = flow(client, peer, port)
            result[case][lane] = {
                "data": counts([p for p in tx if int(p["tcp.len"])],
                               [p for p in flow(server, port, peer) if int(p["tcp.len"])]),
                "acks": counts([p for p in flow(server, peer, port) if int(p["tcp.flags"], 0) & 16],
                               [p for p in rx if int(p["tcp.flags"], 0) & 16]),
            }
            if lane == "netstack":
                samples = [json.loads(line) for line in archive.read(stem + "-netstack-samples.jsonl").splitlines()]
                samples = [row for row in samples if row["port"] == port]
                result[case][lane]["isolated_combined"] = witnesses(tx, rx, samples)
    return result


if __name__ == "__main__":
    with zipfile.ZipFile(sys.argv[1]) as archive:
        print(json.dumps(report(archive), indent=2))
