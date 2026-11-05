"""Check the reconstructed pre-fix timer on the qualified modern source."""
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET


ROOT = Path(os.environ["RUNNER_TEMP"], "persist")
CASE = "TestZeroWindowProbeDuringWrites"
PREFIX = "//test/packetimpact/tests:tcp_zero_window_probe_"
DURATION = r"((?:[0-9]+(?:\.[0-9]+)?(?:ns|µs|ms|s|m|h))+)"
SCALE = {"ns": 1, "µs": 1000, "ms": 1000000, "s": 1000000000,
         "m": 60000000000, "h": 3600000000000}
PROBLEMS = ("No space left on device", "no such package", "no such target", "Error downloading",
            "undefined reference", "No such file or directory", "Operation not permitted",
            "Permission denied", "failed to create a new namespace", "setting up network",
            "connection reset by peer", "DeadlineExceeded", "RESOURCE_EXHAUSTED",
            "command timed out", "failed to fetch", "failed to build", "no matching toolchains",
            "failed to apply", "Unrecognized option", "TCP_INFO returned RTO", "send returned",
            "unknown revision", "Internal error", "unrecognized command-line option",
            "did not build successfully", "Failed to fetch", "exit status")


def nanos(value):
    return int(sum(Decimal(number) * SCALE[unit] for number, unit in
                   re.findall(r"([0-9]+(?:\.[0-9]+)?)(ns|µs|ms|s|m|h)", value)))


def diagnostics(data):
    rows = []
    for line in data.decode(errors="replace").splitlines():
        words = [term for term in PROBLEMS if term.lower() in line.lower()]
        source = re.search(r"(?:^|/|\s)([A-Za-z0-9_.-]+\.(?:go|cc|bzl)|BUILD(?:\.bazel)?):(\d+)", line)
        if words or (source and "error" in line.lower()):
            rows.append({"reasons": words, "source": source[1] + ":" + source[2] if source else None})
        if len(rows) == 6:
            break
    return rows


def read_text(d, name):
    return (d / name).read_text().strip()


def inspect(arm, row):
    d = ROOT / "records" / arm
    if not (d / "exit").exists():
        return
    source, target = read_text(d, "source"), read_text(d, "target")
    row.update(source=source, sender=read_text(d, "sender"), target=target,
               native_exit=int(read_text(d, "exit")), started=read_text(d, "start"),
               execution_finished_at=read_text(d, "finish"), bazel=read_text(d, "version"))
    console = (d / "console").read_bytes()
    row["build_diagnostics"] = diagnostics(console)
    progress = re.findall(rb"\[([0-9,]+) / ([0-9,]+)\]", console)
    if progress:
        done, total = progress[-1]
        row["last_action_progress"] = [int(done.replace(b",", b"")), int(total.replace(b",", b""))]
    row["configured_events"] = 0
    row["completed_events"] = 0
    row["reported_phases"] = [phase for phase in ("Computing main repo mapping", "Loading", "Analyzing", "Executing", "Building")
                                   if phase.encode() + b":" in console]
    row["source_matched"] = (arm == "reconstructed" and target == PREFIX + "netstack_test" and
        source == os.environ["CONTROL"] and row["sender"] == "ead74d5de7f221142b8e74cef7cc241dbbdbdeee" and
        row["bazel"] == "bazel 9.3.1")
    attempts, summaries, starts, finishes = [], [], [], []
    row["failed_actions"] = []
    with (d / "bep").open() as f:
        for line in f:
            try:
                event = json.loads(line)
            except ValueError:
                row["torn_event"] = True
                continue
            ident = event.get("id", {})
            if "targetConfigured" in ident:
                row["configured_events"] += 1
            if "targetCompleted" in ident:
                row["completed_events"] += 1
            if "started" in event:
                uuid = event["started"].get("uuid", "")
                if re.fullmatch(r"[0-9a-f-]{36}", uuid):
                    starts.append(uuid)
            elif "finished" in event:
                finishes.append(event["finished"].get("exitCode", {}).get("code", 0))
            elif "action" in event and not event["action"].get("success") and len(row["failed_actions"]) < 6:
                action = event["action"]
                label = ident.get("actionCompleted", {}).get("label", "")
                mnemonic = action.get("type", "")
                row["failed_actions"].append({
                    "label": label if re.fullmatch(r"[@A-Za-z0-9/._:+~-]{1,180}", label) else None,
                    "mnemonic": mnemonic if re.fullmatch(r"[A-Za-z0-9_]{1,72}", mnemonic) else None,
                    "exit": action.get("exitCode"),
                })
            elif ident.get("testResult", {}).get("label") == target:
                attempts.append((ident["testResult"], event["testResult"]))
            elif ident.get("testSummary", {}).get("label") == target:
                summaries.append(event["testSummary"]["overallStatus"])
    row.update(invocations=starts, bazel_finish=finishes, test_attempts=len(attempts), summaries=summaries)
    if len(attempts) != 1:
        return
    ident, attempt = attempts[0]
    info = attempt.get("executionInfo", {})
    row["attempt"] = {"status": attempt.get("status"), "strategy": info.get("strategy"),
                      "cached": bool(attempt.get("cachedLocally") or info.get("cachedRemotely")),
                      "configuration": ident.get("configuration", {}).get("id"),
                      "run": ident.get("run", 1), "shard": ident.get("shard", 1),
                      "attempt": ident.get("attempt", 1)}
    outputs = {}
    row["reports"] = {}
    for name in ("test.log", "test.xml"):
        path = d / name
        if path.stat().st_size > 6 * 1024 * 1024:
            return
        data = path.read_bytes()
        outputs[name] = data
        digest = hashlib.sha256(data).hexdigest()
        original = [item for item in attempt.get("testActionOutput", []) if item.get("name") == name]
        matches = len(original) == 1 and original[0].get("uri", "").endswith(f"/blobs/{digest}/{len(data)}")
        row["reports"][name] = {"bytes": len(data), "sha256": digest, "cas_match": matches}
    log = outputs["test.log"].decode(errors="replace").replace("\x16", "")
    row["test_diagnostics"] = diagnostics(outputs["test.log"])
    row["go_runs"] = len(re.findall(r"(?m)^=== RUN\s+" + CASE + r"\s*$", log))
    row["go_results"] = re.findall(r"(?m)^--- (PASS|FAIL|SKIP): " + CASE + r" \(", log)
    reads = re.findall(r"(received|no) first probe after ([0-9]+) writes in " + DURATION +
                       r" \(RTO " + DURATION + r"\)", log)
    row["observations"] = [{"probe": disposition == "received", "writes": int(writes),
                            "elapsed_ns": nanos(elapsed), "rto_ns": nanos(rto)}
                           for disposition, writes, elapsed, rto in reads]
    row["longest_gap_ns"] = [nanos(value) for value in re.findall(
        r"longest write gap including the final packet wait: " + DURATION, log)]
    xml = list(ET.fromstring(outputs["test.xml"]).iter("testcase"))
    named = []
    for case in xml:
        if case.get("name") == CASE:
            named.append("SKIP" if case.find("skipped") is not None or case.get("status") == "notrun"
                         or case.get("result") == "suppressed" else
                         "FAIL" if case.find("error") is not None or case.find("failure") is not None else "PASS")
    row["xml_total"], row["named_xml"] = len(xml), named
    marker = {"PASSED": "PASS", "FAILED": "FAIL"}.get(attempt.get("status"))
    if (row["source_matched"] and not row.get("torn_event") and marker and row["native_exit"] == (3 if marker == "FAIL" else 0)
            and finishes == [row["native_exit"]] and len(starts) == 1 and summaries == [attempt["status"]]
            and info.get("strategy") == "remote" and not row["attempt"]["cached"]
            and all(row["attempt"][key] == 1 for key in ("run", "shard", "attempt"))
            and re.fullmatch("[0-9a-f]{64}", row["attempt"]["configuration"] or "")
            and all(r["cas_match"] for r in row["reports"].values()) and len(xml) == 1 and named == [marker]
            and row["go_runs"] == 1 and row["go_results"] == [marker]):
        row["observed_case"] = marker
        if len(reads) != 1 or len(row["longest_gap_ns"]) != 1:
            return
        observation, = row["observations"]
        rto = observation["rto_ns"]
        row["cadence_verified"] = observation["writes"] >= 2 and 0 < row["longest_gap_ns"][0] < rto
        if marker == "PASS" and row["cadence_verified"] and observation["probe"]:
            row["status"] = "UNEXPECTED_PASS"
        elif (marker == "FAIL" and row["cadence_verified"] and not observation["probe"] and
              observation["elapsed_ns"] >= max(4 * rto, 2_000_000_000)):
            row["status"] = "EXPECTED_FAILURE"


reconstructed = {"status": "INCOMPLETE"}
arms = {"reconstructed": reconstructed}
try:
    inspect("reconstructed", reconstructed)
except (OSError, ValueError, KeyError, TypeError, ET.ParseError) as error:
    reconstructed["collection_error"] = type(error).__name__
verified = reconstructed["status"] == "EXPECTED_FAILURE"
unexpected = [arm for arm, row in arms.items() if row["status"].startswith("UNEXPECTED_")]
finished = max((row["execution_finished_at"] for row in arms.values() if "execution_finished_at" in row), default=None)
now = datetime.now(timezone.utc)
result = {"status": "PASS" if verified else "FAIL" if unexpected else "INCOMPLETE", "arms": arms, "case": CASE,
          "historical_function": {"source": "5b40a50016210478f13e725d7c22bd09f71b5ea9",
                                  "bytes": 399, "sha256": "d55541f911e6f801173e563b47976671979701ed6baaaff59cc73366e2a267a6"},
          "literal_stock_retry": {"run": 41999508028, "artifact": 13401971676,
                                 "zip_sha256": "d4443ed711b8f72680bdf8907ac1fbd5dd34cb0822cb7cd4b91c69000746dc2b",
                                 "analysis_exit": 1, "test_attempts": 0},
          "previous": {"run": 41993496783, "artifact": 13400808441,
                       "zip_sha256": "10e9ce401f58da1a183b87dcc275a07722740c0b6aba179235b50febd3d87184",
                       "verified_positive_arms": ["native", "fixed"], "stock_exit_before_test": 124},
          "carrier": os.environ["GITHUB_SHA"], "fixture_blob": "3bff303f79e9d546b16ed052bfcedba621ff702d",
          "placement_blob": "f1a56b130ff2e42ff7b746cf649c70dae244dce1", "expected": list(arms),
          "executed": [name for name, row in arms.items() if row.get("test_attempts")],
          "failed": [name for name, row in arms.items() if row.get("native_exit") not in (None, 0)],
          "unexpected": unexpected,
          "missing": [name for name, row in arms.items() if not row.get("go_results")],
          "execution_finished_at": finished, "verified_at": now.isoformat(),
          "execution_to_verdict_seconds": (now - datetime.fromisoformat(finished)).total_seconds() if finished else None}
(ROOT / "out/verdict.json").write_text(json.dumps(result, indent=2) + "\n")
raise SystemExit(not verified)
