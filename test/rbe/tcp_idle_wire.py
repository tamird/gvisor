"""Check the original native, stock and corrected PacketImpact results."""
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

from qualification_report import xml_cases

ROOT = Path(os.environ["RUNNER_TEMP"])
PROOF = ROOT / "proof"
EXPECTED = {"TestTCPInfo", "TestIdleRestart", "TestIdleRestart/quiet", "TestIdleRestart/reverse"}
ARMS = ("native1", "stock1", "fixed1", "fixed2", "stock2", "native2", "owners")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


OWNERS = {"//pkg/tcpip/transport/tcp/test/e2e:tcp_acktiming_test", "//pkg/tcpip/transport/tcp:tcp_nogo",
          "//test/packetimpact/tests:tcp_retransmits_netstack_test"}


def check_target(arm: str, label: str, dest: Path, events: list[dict], result: dict) -> None:
    actual = {label}
    for e in events:
        if e["id"].get("targetCompleted", {}).get("label") == label:
            for child in e.get("children", []):
                for key in ("targetCompleted", "testSummary", "testResult"):
                    if name := child.get(key, {}).get("label"):
                        actual.add(name)
    attempts = [e["testResult"] for e in events if e["id"].get("testResult", {}).get("label") in actual]
    summaries = [e["testSummary"]["overallStatus"] for e in events if e["id"].get("testSummary", {}).get("label") in actual]
    result["attempts"] = [{"status": t.get("status"), "strategy": t.get("executionInfo", {}).get("strategy"),
                           "cached": bool(t.get("cachedLocally") or t.get("executionInfo", {}).get("cachedRemotely")),
                           "start": t.get("testAttemptStart") or t.get("testAttemptStartMillisEpoch"),
                           "duration": t.get("testAttemptDuration") or t.get("testAttemptDurationMillis")} for t in attempts]
    result["summaries"] = summaries
    expect = "FAILED" if arm.startswith("stock") else "PASSED"
    xml_path = dest / "test.xml"
    xml = xml_path.read_bytes()
    log = (dest / "test.log").read_bytes()
    if arm != "owners":
        if xml:
            (PROOF / (arm + ".xml")).write_bytes(xml)
        if log:
            (PROOF / (arm + ".log")).write_bytes(log)
    reports = {}
    for name, data in (("test.log", log), ("test.xml", xml)):
        reports[name] = {"size": len(data), "sha256": digest(data)}
        for attempt in attempts:
            for original in attempt.get("testActionOutput", []):
                if original.get("name") != name:
                    continue
                uri = original.get("uri", "")
                if "/blobs/" in uri:
                    parts = uri.rsplit("/blobs/", 1)[1].split("/")
                    reports[name]["cas_match"] = parts == [digest(data), str(len(data))]
    result["original_reports"] = reports
    census = xml_cases(xml_path)
    result["counts"] = {kind: census[kind] for kind in ("passed", "failed", "skipped")}
    verified = (len(attempts) == 1 and summaries == [expect] and result["attempts"][0]["status"] == expect
                and result["attempts"][0]["strategy"] == "remote" and not result["attempts"][0]["cached"]
                and bool(log) and all(r.get("cas_match", True) for r in reports.values()))
    named = {}
    for case in ElementTree.fromstring(xml).iter("testcase"):
        name = case.get("name", "")
        assert name and name not in named
        if case.find("failure") is not None or case.find("error") is not None:
            named[name] = "FAILED"
        elif case.find("skipped") is not None or case.get("status") == "notrun" or case.get("result") == "suppressed":
            named[name] = "SKIPPED"
        else:
            named[name] = "PASSED"
    result["named"] = named
    if arm == "owners":
        if label.endswith(":tcp_acktiming_test"):
            verified &= {"TestSRTTNotInflatedByProcessingDelayNoTS", "TestSRTTAfterTailLossProbe"} <= named.keys()
        if label.endswith(":tcp_retransmits_netstack_test"):
            verified &= named == {"TestRetransmits": "PASSED"}
        result["verified"] = verified and census["passed"] > 0 and not census["failed"] and not census["skipped"]
        return
    outcomes = dict.fromkeys(EXPECTED, "PASSED")
    if arm.startswith("stock"):
        outcomes["TestIdleRestart/reverse"] = outcomes["TestIdleRestart"] = "FAILED"
        match = re.search(rb"resumed congestion window = ([0-9]+), want initial ([0-9]+)", log)
        if match:
            cwnd, iw = map(int, match.groups())
            result["stock_diagnostic"] = match[0].decode()
            verified &= cwnd > iw > 0
        else:
            verified = False
    result["verified"] = verified and named == outcomes and not census["skipped"]



def read_arm(arm: str) -> dict:
    dest = ROOT / "records" / arm
    expected = OWNERS if arm == "owners" else {"//test/packetimpact/tests:tcp_info_" + ("native" if arm.startswith("native") else "netstack") + "_test"}
    result = {"verified": False, "expected_labels": sorted(expected), "expected_cases": sorted(EXPECTED) if arm != "owners" else [],
              "executed": [], "targets": {}}
    if not (dest / "exit").exists():
        return result | {"observation": "NOT_RUN"}
    code = int((dest / "exit").read_text())
    result.update(bazel_exit=code, source=(dest / "source").read_text().strip(),
                  started=(dest / "start").read_text().strip(), finished=(dest / "finish").read_text().strip())
    try:
        assert result["source"] == os.environ["MASTER" if arm.startswith("stock") else "PRODUCT"]
        labels = (dest / "labels").read_text().splitlines()
        buildlog = (dest / "build.log").read_text(errors="replace")
        errors = [line[:560] for line in buildlog.splitlines() if line.startswith("ERROR:") and
                  not re.search(r"credential|token|api[-_]?key|header=|https?://|authorization", line, re.I)]
        if code:
            result["errors"] = errors[:5]
        events = []
        with (dest / "bep").open() as stream:
            for line in stream:
                event = json.loads(line)
                if event.keys() & {"started", "finished", "testResult", "testSummary", "completed"}:
                    events.append(event)
        result["invocations"] = [e["started"]["uuid"] for e in events if "started" in e]
        result["bazel_finish"] = [e["finished"].get("finishTime") or e["finished"].get("finishTimeMillis") for e in events if "finished" in e]
        for label in labels:
            target = {"verified": False}
            result["targets"][label] = target
            try:
                check_target(arm, label, dest / label.rsplit(":", 1)[1], events, target)
            except (AssertionError, OSError, ValueError, KeyError, ElementTree.ParseError) as error:
                target["collection_error"] = type(error).__name__
            if target.get("attempts"):
                result["executed"].append(label)
        result["verified"] = (len(labels) == len(expected) and set(labels) == expected
                              and code == (3 if arm.startswith("stock") else 0) and len(result["invocations"]) == 1
                              and len(result["bazel_finish"]) == 1 and all(t["verified"] for t in result["targets"].values()))
    except (OSError, ValueError, KeyError) as error:
        result["collection_error"] = type(error).__name__
    return result


arms = {arm: read_arm(arm) for arm in ARMS}
finished = datetime.now(timezone.utc)
for row in arms.values():
    if finish := row.get("finished"):
        row["execution_to_verdict_seconds"] = (finished - datetime.fromisoformat(finish)).total_seconds()
record = {"carrier": os.environ["GITHUB_SHA"], "stock_control": os.environ["MASTER"], "candidate": os.environ["PRODUCT"],
          "fixture_sha256": digest((ROOT / "fixture.go").read_bytes()), "qualifier_exec_patch_sha256": digest((ROOT / "exec.patch").read_bytes()),
          "wire_experiment": {"run": 41408415755, "artifact": 13123749763},
          "expected": list(arms), "executed": [name for name, row in arms.items() if row["executed"]],
          "arms": arms, "verified_at": finished.isoformat(), "verified": all(row["verified"] for row in arms.values())}
(PROOF / "verdict.json").write_text(json.dumps(record, indent=2) + "\n")
print("ORIGINAL_VERDICT " + json.dumps(record, separators=(",", ":")))
raise SystemExit(not record["verified"])
