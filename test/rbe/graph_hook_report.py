"""Reconcile the original graph result against the seven published hook cases."""
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
LEAVES = {"struct_keys", "struct_values", "array_keys", "array_values", "interface_keys",
          "interface_values", "map_passed_to_LoadValue"}
EARLY = CASE + "/map_passed_to_LoadValue"


def original() -> dict:
    folder = ROOT / "graph"
    row = {"expected_case": CASE, "verified": False}
    try:
        for name in ("source", "start", "finish", "exit"):
            row[name] = (folder / name).read_text().strip()
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
            report = file_record(PROOF / ("graph." + suffix))
            output, = [f for f in attempt["testActionOutput"] if f["name"] == "test." + suffix]
            uri = output.get("uri", "")
            report["original_match"] = (uri.endswith("/blobs/{sha256}/{bytes}".format(**report))
                                        or (output.get("digest") == report["sha256"] and str(output.get("length")) == str(report["bytes"])))
            row["reports"][suffix] = report
        path = PROOF / "graph.xml"
        census = xml_cases(path)
        row["counts"] = {key: census[key] for key in ("passed", "skipped", "failed")}
        expected_names = {CASE} | {CASE + "/" + leaf for leaf in LEAVES}
        cases = list(ElementTree.parse(path).iter("testcase"))
        row["actual_cases"] = [c.get("name") for c in cases]
        failures = {c.attrib["name"] for c in cases if c.find("failure") is not None or c.find("error") is not None}
        completed = (len(cases) == len(expected_names) and set(row["actual_cases"]) == expected_names
                     and row["counts"]["skipped"] == 0
                     and all(c.get("status") != "notrun" and c.get("result") != "suppressed" for c in cases))
        log = (PROOF / "graph.log").read_text()
        discrepancy = [line.strip() for line in log.splitlines() if "loaded map = " in line]
        early_only = (failures == {CASE, EARLY} and len(discrepancy) == 1
                      and "inline:tests.mapLoadHook{value:0}" in discrepancy[0].partition(", want ")[0]
                      and "inline:tests.mapLoadHook{value:2}" in discrepancy[0].partition(", want ")[2])
        row["failed_cases"] = sorted(failures)
        row["early_copy_diagnostic"] = discrepancy
        row["observation"] = "ALL_PASS" if not failures else ("EARLY_HOOK_READ_ZERO" if early_only else "OTHER_FAILURE")
        expected = "FAILED" if failures else "PASSED"
        row["verified"] = (row["source"] == os.environ["GITHUB_SHA"] and bool(row["invocation"])
                           and bool(row["bazel_finished"]) and int(row["exit"]) == (3 if failures else 0)
                           and status == row["attempts"] == [expected] and row["strategy"] == "remote" and not row["cached"]
                           and all(r["original_match"] and r["bytes"] for r in row["reports"].values())
                           and completed and (not failures or early_only))
    except (AssertionError, ValueError, KeyError, OSError, ElementTree.ParseError) as error:
        row["collection_error"] = type(error).__name__
    return row


arm = original()
finished = datetime.now(timezone.utc)
if finish := arm.get("finish"):
    arm["execution_to_verdict_seconds"] = (finished - datetime.fromisoformat(finish)).total_seconds()
record = {"source": os.environ["GITHUB_SHA"], "upstream": os.environ["MASTER"], "fixture": os.environ["FIXTURE"],
          "owner": OWNER, "graph": arm, "verified_at": finished.isoformat(), "verified": arm["verified"]}
(PROOF / "verdict.json").write_text(json.dumps(record, indent=2) + "\n")
print("ORIGINAL_GRAPH_HOOK_VERDICT " + json.dumps(record, separators=(",", ":")))
raise SystemExit(not record["verified"])
