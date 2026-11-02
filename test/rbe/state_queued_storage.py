"""Reconcile the original queued-indirect conversion test on each source."""
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
SIGNATURE = "queued conversion aliases changed:"


def original(arm: str) -> dict:
    folder = ROOT / arm
    row = {"expected_case": CASE, "verified": False}
    try:
        for name in ("source", "fixture", "start", "finish", "exit"):
            row[name] = (folder / name).read_text().strip()
        expected = "FAILED" if arm == "unsafe" else "PASSED"
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
        row["actual_cases"] = [c.get("name") for c in ElementTree.parse(path).iter("testcase")]
        row["alias_failure_diagnostic"] = SIGNATURE in (PROOF / (arm + ".log")).read_text()
        count = int(arm == "unsafe")
        row["verified"] = (row["source"] == os.environ["CONTROL" if count else "PRODUCT"] and bool(row["invocation"])
                           and bool(row["bazel_finished"]) and int(row["exit"]) == (3 if count else 0)
                           and status == row["attempts"] == [expected] and row["strategy"] == "remote" and not row["cached"]
                           and all(r["original_match"] and r["bytes"] for r in row["reports"].values())
                           and row["counts"] == {"passed": 1 - count, "skipped": 0, "failed": count}
                           and row["actual_cases"] == [CASE] and row["alias_failure_diagnostic"] == bool(count))
    except (AssertionError, ValueError, KeyError, OSError, ElementTree.ParseError) as error:
        row["collection_error"] = type(error).__name__
    return row


arms = {arm: original(arm) for arm in ("unsafe", "fixed")}
finished = datetime.now(timezone.utc)
for arm in arms.values():
    if finish := arm.get("finish"):
        arm["execution_to_verdict_seconds"] = (finished - datetime.fromisoformat(finish)).total_seconds()
record = {"source": os.environ["GITHUB_SHA"], "owner": OWNER, "arms": arms, "verified_at": finished.isoformat(),
          "verified": all(a["verified"] for a in arms.values()) and arms["unsafe"]["fixture"] == arms["fixed"]["fixture"]}
(PROOF / "verdict.json").write_text(json.dumps(record, indent=2) + "\n")
print("QUEUED_STORAGE_VERDICT " + json.dumps(record, separators=(",", ":")))
raise SystemExit(not record["verified"])
