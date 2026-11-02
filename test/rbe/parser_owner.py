"""Bind the native parser test's stdout and XML to its original Bazel attempt."""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

from qualification_report import file_record, xml_cases

folder = Path(os.environ["RUNNER_TEMP"]) / "parser"
out = folder / "records"
owner = os.environ["OWNER"]
read = lambda name: (folder / name).read_text().strip()
row = {"source": read("source"), "owner": owner, "start": read("start"), "finish": read("finish"),
       "exit": int(read("exit")), "verdict": "FAIL" if int(read("exit")) else "INCOMPLETE"}
dest = out / "verdict.json"
dest.write_text(json.dumps(row))
try:
    attempts = []
    summaries = []
    completed = []
    failures = []
    with (folder / "bep").open() as stream:
        for line in stream:
            event = json.loads(line)
            eid = event["id"]
            if "started" in event:
                row["invocation"] = event["started"]["uuid"]
                row["bazel"] = event["started"]["buildToolVersion"]
            if "finished" in event:
                row["build_finish"] = event["finished"].get("finishTimeMillis") or event["finished"].get("finishTime")
                row["build_exit"] = event["finished"]["exitCode"].get("code", 0)
            if eid.get("targetCompleted", {}).get("label") == owner and not eid["targetCompleted"].get("aspect"):
                completed.append(event.get("completed", {}).get("success", False))
            if eid.get("testSummary", {}).get("label") == owner:
                summaries.append(event["testSummary"]["overallStatus"])
            if eid.get("testResult", {}).get("label") == owner:
                attempts.append(event["testResult"])
            if action := event.get("action"):
                if not action.get("success", False):
                    failures.append({"label": eid.get("actionCompleted", {}).get("label"), "mnemonic": action.get("type"),
                                     "exit": action.get("exitCode")})
    row.update(attempts=[a.get("status") for a in attempts], completed=completed, summaries=summaries, failed_actions=failures[:8])
    if row["exit"]:
        # Compiler locations and stable failure classes only. Full logs and authenticated Bazel flags stay private.
        text = (folder / "build.log").read_text(errors="replace")
        row["failure_categories"] = sorted(set(re.findall(
            r"RESOURCE_EXHAUSTED|Resource exhausted|Out of memory|Killed signal terminated program cc1plus|"
            r"undefined reference|undeclared inclusion|no matching toolchains|no such target|not defined|"
            r"could not allocate|Remote execution failed|compilation terminated", text, re.I)))
        row["compiler_locations"] = list(dict.fromkeys(re.findall(
            r"(?:^|\n)([A-Za-z0-9_+./-]+\.(?:h|c|cc|cpp):[0-9]+(?::[0-9]+)?): (?:fatal )?error:", text)))[:10]
    assert len(attempts) == 1
    attempt, = attempts
    info = attempt.get("executionInfo", {})
    row.update(strategy=info.get("strategy"), cached=bool(attempt.get("cachedLocally") or info.get("cachedRemotely")), reports={})
    for name in ("test.xml", "test.log"):
        record = file_record(out / name)
        original, = [f for f in attempt["testActionOutput"] if f["name"] == name]
        record["original"] = (original.get("uri", "").endswith("/blobs/{sha256}/{bytes}".format(**record))
                              or (original.get("digest") == record["sha256"] and str(original.get("length")) == str(record["bytes"])))
        row["reports"][name] = record
    census = xml_cases(out / "test.xml")
    row["counts"] = {key: census[key] for key in ("passed", "failed", "skipped")}
    row["cases"] = [c.get("name") for c in ElementTree.parse(out / "test.xml").iter("testcase")]
    if (row["source"] == os.environ["PRODUCT"] and row["bazel"] == "9.3.1" and row["invocation"]
            and row["build_finish"] and row["exit"] == row["build_exit"] == 0 and completed == [True]
            and row["attempts"] == summaries == ["PASSED"] and row["strategy"] == "remote" and not row["cached"]
            and all(r["original"] and r["bytes"] for r in row["reports"].values()) and row["cases"] == ["TestParser"]
            and row["counts"] == {"passed": 1, "failed": 0, "skipped": 0}):
        row["verdict"] = "PASS"
except (AssertionError, KeyError, ValueError, OSError, ElementTree.ParseError) as e:
    row["collection_error"] = type(e).__name__
now = datetime.now(timezone.utc)
row["verified_at"] = now.isoformat()
row["exit_to_verdict_seconds"] = round((now - datetime.fromisoformat(row["finish"])).total_seconds(), 3)
dest.write_text(json.dumps(row, indent=2)+"\n")
print(json.dumps(row))
raise SystemExit(row["verdict"] != "PASS")
