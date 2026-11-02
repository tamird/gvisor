"""Bind each valid Save/Load scan to the original fresh test output."""
import json
import os
import re
from pathlib import Path
from xml.etree import ElementTree

from qualification_report import file_record, xml_cases

d = Path(os.environ["RUNNER_TEMP"]) / "graph"
proof = d.parent / "proof"
owner = os.environ["OWNER"]
case = os.environ["CASE"]
row = {name: (d / name).read_text().strip() for name in ["source", "start", "finish", "exit"]}
row["baseline"] = os.environ["STOCK"]
row["verified"] = False
try:
    assert row["source"] == os.environ["PRODUCT"]
    attempts, summaries = [], []
    for line in (d / "bep").open():
        event = json.loads(line)
        if "started" in event:
            row["invocation"] = event["started"]["uuid"]
        if event["id"].get("testResult", {}).get("label") == owner:
            attempts.append(event["testResult"])
        if event["id"].get("testSummary", {}).get("label") == owner:
            summaries.append(event["testSummary"]["overallStatus"])
    row["summaries"] = summaries
    attempt, = attempts
    info = attempt.get("executionInfo", {})
    row["attempt"] = {"status": attempt["status"], "strategy": info.get("strategy"),
                      "cached": bool(info.get("cachedRemotely") or attempt.get("cachedLocally"))}
    reports = row["reports"] = {}
    for name in ("test.log", "test.xml"):
        report = file_record(proof / name)
        reports[name] = report
        entry, = [e for e in attempt["testActionOutput"] if e["name"] == name]
        assert entry.get("uri", "").endswith("/blobs/{sha256}/{bytes}".format(**report)) or (
            entry.get("digest") == report["sha256"] and str(entry.get("length")) == str(report["bytes"]))
    results = row["cases"] = xml_cases(proof / "test.xml")
    names = [x.get("name") for x in ElementTree.parse(proof / "test.xml").iter("testcase")]
    sizes = (16, 64, 256, 1024)
    expected = {f"{case}/n{n}/seeded_{seed}" for n in sizes for seed in ("true", "false")}
    assert expected <= set(names) <= expected | {case} and len(names) == len(set(names))
    assert results["passed"] == len(names) and not results["failed"] and not results["skipped"]
    pattern = (r"DEFERRED_SCAN n=(\d+) seeded=(true|false) bytes=(\d+) objects=(\d+) "
               r"pending_maps=(\d+) pending_other=(\d+) passes=(\d+) retry_visits=(\d+) scan_sizes=\[([\d ]*)\]")
    counts = row["counts"] = {}
    for n, seed, size, objs, maps, other, passes, visits, scans in re.findall(pattern, (proof / "test.log").read_text()):
        label = f"{case}/n{n}/seeded_{seed}"
        assert label not in counts
        queue = [int(s) for s in scans.split()]
        assert len(queue) == int(passes) and sum(queue) == int(visits)
        assert (queue[0] if queue else 0) == int(maps) + int(other)
        assert all(a > b > 0 for a, b in zip(queue, queue[1:]))
        counts[label] = dict(stream_bytes=int(size), objects=int(objs), pending_maps=int(maps),
                             pending_other=int(other), retry_passes=len(queue), retry_visits=sum(queue))
    assert set(counts) == expected
    row["verified"] = (row["exit"] == "0" and summaries == ["PASSED"] and row["attempt"] ==
                       {"status": "PASSED", "strategy": "remote", "cached": False})
except (AssertionError, KeyError, OSError, ValueError, ElementTree.ParseError) as error:
    row["collection_error"] = type(error).__name__
finally:
    (proof / "verdict.json").write_text(json.dumps(row, indent=2) + "\n")
raise SystemExit(not row["verified"])
