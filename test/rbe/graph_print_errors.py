"""Reconcile the four graph printer cases on the two exact sources."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

from qualification_report import file_record, xml_cases

ROOT = Path(os.environ["RUNNER_TEMP"])
PROOF = ROOT / "proof"
CASE = os.environ["CASE"]
OWNER = os.environ["OWNER"]
CONTROL_CASES = {"TestPrintGraphDeterministic", "TestPrintGraphSliceNotation",
                 "TestPrintGraphUnexportedInlineMapAndTrailingData"}


def original(arm: str) -> dict:
    folder = ROOT / arm
    row = {"expected_case": CASE, "verified": False}
    try:
        for name in ("source", "fixture", "start", "finish", "exit"):
            row[name] = (folder / name).read_text().strip()
        expected = "FAILED" if arm == "stock" else "PASSED"
        status = []
        attempts = []
        with (folder / "bep").open() as stream:
            for line in stream:
                event = json.loads(line)
                if "started" in event:
                    row["invocation"] = event["started"]["uuid"]
                if "finished" in event:
                    row["bazel_finished"] = event["finished"].get("finishTime") or event["finished"].get("finishTimeMillis")
                if event["id"].get("testSummary", {}).get("label") == OWNER:
                    status.append(event["testSummary"]["overallStatus"])
                if event["id"].get("testResult", {}).get("label") == OWNER:
                    attempts.append(event["testResult"])
        row["summaries"] = status
        row["attempts"] = [a["status"] for a in attempts]
        assert len(attempts) == 1
        attempt = attempts[0]
        info = attempt.get("executionInfo", {})
        row["strategy"] = info.get("strategy")
        row["cached"] = bool(info.get("cachedRemotely") or attempt.get("cachedLocally"))
        row["reports"] = {}
        for suffix in ("xml", "log"):
            report = file_record(PROOF / (arm + "." + suffix))
            output, = [f for f in attempt["testActionOutput"] if f["name"] == "test." + suffix]
            uri = output.get("uri", "")
            report["original_match"] = (uri.endswith("/blobs/{sha256}/{bytes}".format(**report))
                                        or (output.get("digest") == report["sha256"] and str(output.get("length")) == str(report["bytes"])))
            row["reports"][suffix] = report
        path = PROOF / (arm + ".xml")
        census = xml_cases(path)
        row["counts"] = {key: census[key] for key in ("passed", "skipped", "failed")}
        cases = list(ElementTree.parse(path).iter("testcase"))
        names = [case.attrib["name"] for case in cases]
        expected_names = CONTROL_CASES | {CASE}
        row["actual_cases"] = names
        row["failed_cases"] = [c.attrib["name"] for c in cases if c.find("failure") is not None or c.find("error") is not None]
        log = (PROOF / (arm + ".log")).read_text()
        row["accepted_output_error"] = [mode for mode in ("false", "true")
                                        if f"PrintGraph(html={mode}) = <nil>, want closed pipe" in log]
        count = int(arm == "stock")
        row["verified"] = (row["source"] == os.environ["CONTROL" if count else "PRODUCT"] and bool(row["invocation"])
                           and bool(row["bazel_finished"]) and int(row["exit"]) == (3 if count else 0)
                           and status == row["attempts"] == [expected] and row["strategy"] == "remote" and not row["cached"]
                           and all(r["original_match"] and r["bytes"] for r in row["reports"].values())
                           and row["counts"] == {"passed": len(expected_names) - count, "skipped": 0, "failed": count}
                           and len(names) == len(expected_names) and set(names) == expected_names
                           and all(c.get("status") != "notrun" and c.get("result") != "suppressed" for c in cases)
                           and row["failed_cases"] == ([CASE] if count else [])
                           and row["accepted_output_error"] == (["false", "true"] if count else []))
    except (AssertionError, ValueError, KeyError, OSError, ElementTree.ParseError) as error:
        row["collection_error"] = type(error).__name__
    return row


arms = {arm: original(arm) for arm in ("stock", "fixed")}
finished = datetime.now(timezone.utc)
for arm in arms.values():
    if finish := arm.get("finish"):
        arm["execution_to_verdict_seconds"] = (finished - datetime.fromisoformat(finish)).total_seconds()
record = {"source": os.environ["GITHUB_SHA"], "upstream": os.environ["STOCK"], "owner": OWNER, "arms": arms, "verified_at": finished.isoformat(),
          "verified": all(a["verified"] for a in arms.values()) and arms["stock"]["fixture"] == arms["fixed"]["fixture"]}
(PROOF / "verdict.json").write_text(json.dumps(record, indent=2) + "\n")
print("GRAPH_PRINT_VERDICT " + json.dumps(record, separators=(",", ":")))
raise SystemExit(not record["verified"])
