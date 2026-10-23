#!/usr/bin/env python3
# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Reduce native receiver intervals without assuming their start is synchronized."""

import csv
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import statistics
import sys


def number(value: object) -> float:
    assert isinstance(value, (int, float)) and not isinstance(value, bool), value
    assert math.isfinite(value), value
    return float(value)


def phases(path: Path) -> list[dict[str, str]]:
    with path.open() as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    assert rows, path
    boots = [number(float(row["boottime_seconds"])) for row in rows]
    walls = [number(float(row["unix_seconds"])) for row in rows]
    assert boots == sorted(boots), (path, boots)
    assert walls == sorted(walls), (path, walls)
    return rows


@dataclass
class Receiver:
    earliest_start: float
    latest_start: float
    duration: float
    intervals: list[tuple[float, float, int]]
    total_bytes: int

    def window(self, begin: float, end: float) -> dict[str, object]:
        # Every possible receiver start must cover the entire requested window.
        assert self.latest_start <= begin < end <= self.earliest_start + self.duration, (
            self.earliest_start, self.latest_start, self.duration, begin, end
        )
        lower = upper = contained = possible = 0
        for start, stop, count in self.intervals:
            if begin <= self.earliest_start + start and self.latest_start + stop <= end:
                lower += count
                contained += 1
            if self.latest_start + stop > begin and self.earliest_start + start < end:
                upper += count
                possible += 1
        assert 0 <= lower <= upper <= self.total_bytes, (lower, upper, self.total_bytes)
        assert contained > 0, (begin, end, self)
        return {
            "bytes_bounds": [lower, upper],
            "bits_per_second_bounds": [lower * 8 / (end - begin), upper * 8 / (end - begin)],
            "whole_intervals_contained": contained,
            "intervals_possibly_overlapping": possible,
        }


def receiver(directory: Path) -> Receiver:
    rows = phases(directory / "flow-phases.tsv")
    assert [row["phase"] for row in rows] == [
        "proxy-start", "client-operation-begin", "client-operation-end"
    ], rows
    begin, end = (float(rows[index]["boottime_seconds"]) for index in (1, 2))
    data = json.loads((directory / "receiver.json").read_text())
    assert "error" not in data, data.get("error")
    assert data["start"]["test_start"]["num_streams"] == 1, data["start"]
    total = data["end"]["sum_received"]
    assert total["sender"] is False, total
    duration = number(total["seconds"])
    assert duration > 0, duration
    # /proc/uptime has 10ms resolution. Expand, rather than tighten, its bounds.
    earliest, latest = begin - 0.02, end + 0.02 - duration
    assert earliest <= latest, (earliest, latest)
    intervals = []
    previous = 0.0
    for row in data["intervals"]:
        summary = row["sum"]
        assert len(row["streams"]) == 1 and summary["sender"] is False, row
        start, stop = number(summary["start"]), number(summary["end"])
        count = summary["bytes"]
        assert isinstance(count, int) and not isinstance(count, bool) and count >= 0, count
        assert abs(start - previous) < 0.001 and stop > start, (start, stop, previous)
        intervals.append((start, stop, count))
        previous = stop
    assert intervals and abs(previous - duration) < 0.001, (previous, duration)
    assert sum(count for _, _, count in intervals) == total["bytes"], total
    assert total["bytes"] == sum(row["receiver"]["bytes"] for row in data["end"]["streams"]), total
    return Receiver(earliest, latest, duration, intervals, total["bytes"])


def qdiscs(path: Path, delay: int) -> dict[str, object]:
    text = path.read_text()
    parts = re.split(r"(?m)^(wan\.[01])\n", text)
    assert len(parts) == 5 and parts[0] == "", (path, parts)
    observed = {}
    for device, body in zip(parts[1::2], parts[2::2]):
        lines = body.strip().splitlines()
        assert re.fullmatch(
            rf"qdisc netem \S+: root refcnt \d+ limit 100 delay {delay}ms rate 20Mbit", lines[0]
        ), (path, lines[0])
        match = re.fullmatch(
            r" Sent (\d+) bytes (\d+) pkt \(dropped (\d+), overlimits (\d+) requeues (\d+)\)\s*",
            lines[1],
        )
        assert match is not None, (path, lines)
        observed[device] = {
            "delay_ms": delay, "rate_mbit": 20, "queue_packets": 100,
            "sent_bytes": int(match[1]), "sent_packets": int(match[2]),
            "dropped_packets": int(match[3]), "overlimits": int(match[4]),
            "requeues": int(match[5]), "backlog": lines[2].strip(),
        }
    assert set(observed) == {"wan.0", "wan.1"}, observed
    return observed


def main() -> None:
    directory = Path(sys.argv[1])
    assert sys.argv[2] in {"true", "false"}, sys.argv[2]
    changing = sys.argv[2] == "true"
    receivers = {flow: receiver(directory / flow) for flow in ("primary", "secondary")}
    flow_rows = {flow: phases(directory / flow / "flow-phases.tsv") for flow in receivers}
    probe_rows = phases(directory / "ping-phases.tsv")
    assert [row["phase"] for row in probe_rows] == [
        "probe-launch", "flows-begin", "flows-end", "probe-stop"
    ], probe_rows
    all_rows = [row for rows in flow_rows.values() for row in rows] + probe_rows
    origin = max(float(rows[1]["boottime_seconds"]) for rows in flow_rows.values())
    configuration = {"before": qdiscs(directory / "qdisc-before.txt", 5 if changing else 50)}
    if changing:
        transitions = phases(directory / "rtt-profile.tsv")
        expected = [("origin", "-")]
        for step in (60, 420):
            for device in ("wan.0", "wan.1"):
                expected.extend([(f"before-{step}", device), (f"after-{step}", device)])
        assert [(row["phase"], row["device"]) for row in transitions] == expected, transitions
        assert float(transitions[0]["boottime_seconds"]) == origin, (transitions, origin)
        all_rows += transitions
        after = {}
        for step, delay, prior in ((60, 50, 5), (420, 5, 50)):
            rows = [row for row in transitions if row["phase"] in {f"before-{step}", f"after-{step}"}]
            assert all(origin + step - 0.02 <= float(row["boottime_seconds"]) <= origin + step + 5 for row in rows), rows
            assert all(row["exit"] == "0" for row in rows if row["phase"] == f"after-{step}"), rows
            after[step] = max(float(row["boottime_seconds"]) for row in rows)
            configuration[f"before-{step}"] = qdiscs(directory / f"qdisc-before-step-{step}.txt", prior)
            configuration[f"after-{step}"] = qdiscs(directory / f"qdisc-after-step-{step}.txt", delay)
        windows = [("low", origin + 20, origin + 40),
                   ("late-high", after[60] + 310, after[60] + 350),
                   ("returned-low", after[420] + 20, after[420] + 40)]
        assert windows[1][2] < origin + 420, windows
    else:
        transitions = []
        windows = [("fixed", origin + 20, origin + 100)]
    configuration["after"] = qdiscs(directory / "qdisc-after.txt", 5 if changing else 50)
    offset_bounds = []
    for row in all_rows:
        before = float(row["boottime_seconds"])
        after = number(float(row["boottime_after_seconds"]))
        wall = float(row["unix_seconds"])
        assert 0 <= after - before <= 1, row
        offset_bounds.append((wall - after - 0.02, wall - before + 0.02))
    # Date is bracketed by uptime reads. Reject phases that cannot share one
    # wall/boot offset; do not invent a point alignment from sequential reads.
    offset_lower = max(lower for lower, _ in offset_bounds)
    offset_upper = min(upper for _, upper in offset_bounds)
    assert offset_lower <= offset_upper, offset_bounds
    ping = [(float(match[1]), float(match[2])) for match in re.finditer(
        r"(?m)^\[([0-9.]+)\].*bytes from 10\.0\.0\.4:.*time=([0-9.]+) ms$",
        (directory / "ping.txt").read_text(),
    )]
    observations = []
    for name, begin, end in windows:
        flows = {flow: record.window(begin, end) for flow, record in receivers.items()}
        # Include only replies within the window for every admissible common offset.
        samples = [rtt for wall, rtt in ping if begin + offset_upper <= wall <= end + offset_lower]
        assert samples, (name, begin, end)
        first_lower, first_upper = flows["primary"]["bytes_bounds"]
        second_lower, second_upper = flows["secondary"]["bytes_bounds"]
        denominator_lower = first_lower + second_upper
        denominator_upper = first_upper + second_lower
        share = [first_lower / denominator_lower if denominator_lower else 0,
                 first_upper / denominator_upper if denominator_upper else 1]
        observations.append({
            "name": name, "boottime_window": [begin, end], "flows": flows,
            "primary_share_bounds": share,
            "icmp": {"count": len(samples), "min_ms": min(samples),
                     "median_ms": statistics.median(samples), "max_ms": max(samples)},
        })
    # Preserve observations even if loaded-queue ICMP cannot distinguish the steps.
    visible = None
    if changing:
        low, high, returned = [row["icmp"]["min_ms"] for row in observations]
        visible = high >= low + 40 and high >= returned + 40
    print(json.dumps({
        "status": "PASS", "changing": changing,
        "origin": "later client-operation-begin, before connection retry",
        "receiver_start_bounds": {flow: [record.earliest_start, record.latest_start] for flow, record in receivers.items()},
        "qdiscs": configuration, "transitions": transitions, "windows": observations,
        "observed_clock_offset_bounds": offset_bounds,
        "admissible_common_clock_offset": [offset_lower, offset_upper],
        "icmp_steps_distinguishable": visible,
        "limits": "One feasibility pair; no inferential performance or sender-minRTT-expiry claim. ICMP observes the shared native path. Qdisc drops are observed, not configured random loss. Receiver bounds include complete boundary intervals, never interpolated bytes.",
    }, indent=2))


if __name__ == "__main__":
    main()
