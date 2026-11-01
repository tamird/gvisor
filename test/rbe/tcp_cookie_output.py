"""Reconcile the original declined cookie outputs and the final TCP analyzer."""
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
SIGNATURE = "generated SYN cookies with failed output = 0, want 1"


def original(arm: str) -> dict:
    folder = ROOT / arm
    analyzer = arm == "analyzer"
    owner = os.environ["NOGO"] if analyzer else OWNER
    row = {"owner": owner, "verified": False}
    try:
        for name in ("source", "fixture", "start", "finish", "exit"):
            row[name] = (folder / name).read_text().strip()
        expected = "FAILED" if arm == "prior" else "PASSED"
        status = []
        attempts = []
        with (folder / "bep").open() as stream:
            for line in stream:
                event = json.loads(line)
                if "started" in event:
                    row["invocation"] = event["started"]["uuid"]
                if "finished" in event:
                    row["bazel_finished"] = event["finished"].get("finishTime") or event["finished"].get("finishTimeMillis")
                if event["id"].get("testSummary", {}).get("label") == owner:
                    status.append(event["testSummary"]["overallStatus"])
                if event["id"].get("testResult", {}).get("label") == owner:
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
        log = (PROOF / (arm + ".log")).read_text()
        if analyzer:
            row["analyzer_lines"] = [line for line in log.splitlines() if line in ("amd64...PASS", "arm64...PASS")]
            complete = sorted(row["analyzer_lines"]) == ["amd64...PASS", "arm64...PASS"] and census["passed"] > 0 and not census["failed"] and not census["skipped"]
        else:
            row["refused_output_crossed"] = SIGNATURE in log if arm == "prior" else row["counts"] == {"passed": 1, "skipped": 0, "failed": 0}
            complete = row["actual_cases"] == [CASE] and row["refused_output_crossed"]
        count = int(arm == "prior")
        row["verified"] = (row["source"] == os.environ[{"prior": "CONTROL", "fixed": "FIXED", "analyzer": "PRODUCT"}[arm]] and bool(row["invocation"])
                           and bool(row["bazel_finished"]) and int(row["exit"]) == (3 if count else 0)
                           and status == row["attempts"] == [expected] and row["strategy"] == "remote" and not row["cached"]
                           and all(r["original_match"] and r["bytes"] for r in row["reports"].values())
                           and (analyzer or row["counts"] == {"passed": 1 - count, "skipped": 0, "failed": count}) and complete)
    except (AssertionError, ValueError, KeyError, OSError, ElementTree.ParseError) as error:
        row["collection_error"] = type(error).__name__
    return row


arms = {arm: original(arm) for arm in ("prior", "fixed", "analyzer")}
finished = datetime.now(timezone.utc)
for arm in arms.values():
    if finish := arm.get("finish"):
        arm["execution_to_verdict_seconds"] = (finished - datetime.fromisoformat(finish)).total_seconds()
record = {"source": os.environ["GITHUB_SHA"], "owner": OWNER, "arms": arms, "verified_at": finished.isoformat(),
          "verified": all(a["verified"] for a in arms.values()) and arms["prior"]["fixture"] == arms["fixed"]["fixture"]}
(PROOF / "verdict.json").write_text(json.dumps(record, indent=2) + "\n")
print("ORIGINAL_COOKIE_VERDICT " + json.dumps(record, separators=(",", ":")))
raise SystemExit(not record["verified"])
