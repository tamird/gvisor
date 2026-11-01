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
EXPECTED = {"TestNoDuplicateSYN", "TestPassiveDuplicateSYN"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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
    expect = "FAILED" if arm == "stock" else "PASSED"
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
        name = case.get("name", "").rsplit("/", 1)[-1]
        if name in EXPECTED:
            assert name not in named, "duplicate required case"
            if case.find("failure") is not None or case.find("error") is not None:
                named[name] = "FAILED"
            elif case.find("skipped") is not None or case.get("status") == "notrun" or case.get("result") == "suppressed":
                named[name] = "SKIPPED"
            else:
                named[name] = "PASSED"
    result["executed_cases"] = [name for name, status in named.items() if status != "SKIPPED"]
    result["named"] = named
    pattern = rb"DUPSYN duplicate=(true|false) responses=\[([^]]*)\] child=(\d+) listener=(\d+) handshake_us=(\d+)"
    observations = {dup.decode(): [resp.decode(), int(child), int(listener), int(elapsed)] for dup, resp, child, listener, elapsed in set(re.findall(pattern, log))}
    result["wire"] = observations
    result["verified"] = (verified and named == {"TestNoDuplicateSYN": "PASSED", "TestPassiveDuplicateSYN": expect}
                          and census["passed"] == (2 if arm == "native" else 1)
                          and census["failed"] == (0 if arm == "native" else 1) and not census["skipped"]
                          and len(observations) == 2 and observations["false"][:3] == ["", 0, 0]
                          and observations["true"][:3] == (["SYN-ACK", 1, 0] if arm == "native" else ["ACK", 0, 0])
                          and all(0 < v[3] < 800_000 for v in observations.values()))


def read_arm(arm: str) -> dict:
    dest = ROOT / "records" / arm
    expected = {"//test/packetimpact/tests:tcp_duplicate_syn_" + ("native" if arm == "native" else "netstack") + "_test"}
    result = {"verified": False, "expected_labels": sorted(expected), "expected_cases": list(EXPECTED) if arm != "owners" else [],
              "executed": [], "targets": {}}
    if not (dest / "exit").exists():
        return result | {"observation": "NOT_RUN"}
    code = int((dest / "exit").read_text())
    result.update(bazel_exit=code, source=(dest / "source").read_text().strip(),
                  started=(dest / "start").read_text().strip(), finished=(dest / "finish").read_text().strip())
    try:
        with (dest / "build.log").open(errors="replace") as f:
            result["diagnostics"] = [line.strip()[:400] for line in f if re.search(r"^(?:ERROR:|[^ ]+\.go:\d+|[^ ]+ is not declared in ALL_TESTS)", line) and not re.search(r"(?i)https?:|grpcs:|header|api.key|token|bearer|exec env", line)][:12]
        labels = (dest / "labels").read_text().splitlines()
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
                              and code == (3 if arm == "stock" else 0) and len(result["invocations"]) == 1
                              and len(result["bazel_finish"]) == 1 and all(t["verified"] for t in result["targets"].values()))
    except (OSError, ValueError, KeyError) as error:
        result["collection_error"] = type(error).__name__
    return result


arms = {arm: read_arm(arm) for arm in ("native", "stock")}
finished = datetime.now(timezone.utc)
for row in arms.values():
    if finish := row.get("finished"):
        row["execution_to_verdict_seconds"] = (finished - datetime.fromisoformat(finish)).total_seconds()
record = {"carrier": os.environ["GITHUB_SHA"], "unmodified_product": os.environ["MASTER"], "candidate": os.environ["PRODUCT"],
          "fixture_sha256": digest((ROOT / "fixture.go").read_bytes()), "qualifier_exec_patch_sha256": digest((ROOT / "exec.patch").read_bytes()),
          "expected": list(arms), "executed": [name for name, row in arms.items() if row["executed"]],
          "arms": arms, "verified_at": finished.isoformat(), "verified": all(row["verified"] for row in arms.values())}
(PROOF / "verdict.json").write_text(json.dumps(record, indent=2) + "\n")
print("ORIGINAL_VERDICT " + json.dumps(record, separators=(",", ":")))
raise SystemExit(not record["verified"])
