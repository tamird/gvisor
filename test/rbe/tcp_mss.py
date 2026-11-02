#!/usr/bin/env python3
"""Run one original packetimpact invocation and reconcile its wire observations."""
import base64
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

import qualification_report as report

BASE = "918aefd5f"
FIXTURE = "1b84aa14093a95c224b2a65cd6f0f1fd2ddb2958"
LABELS = [f"//test/packetimpact/tests:tcp_cork_mss_{arm}_test" for arm in ("native", "netstack")]
CASES = {"no_sack", "sack", "sack_receive_gap"}
TEMP = Path(os.environ["RUNNER_TEMP"])
PRIVATE = TEMP / "mss-original"
PUBLIC = TEMP / "mss-report"


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def run():
    PRIVATE.mkdir(exist_ok=True)
    PUBLIC.mkdir(exist_ok=True)
    result = {"status": "INCOMPLETE", "started_at": now(), "labels": LABELS}
    try:
        source = output("git", "rev-parse", "HEAD")
        assert output("git", "rev-parse", "HEAD^") == FIXTURE
        assert output("git", "rev-parse", "HEAD^^") == output("git", "rev-parse", BASE)
        assert output("git", "diff", "HEAD^", "HEAD", "--", ".", ":!.github/workflows/build.yml", ":!test/rbe/tcp_mss.py") == ""
        result["source"] = source
        (PRIVATE / "commit").write_bytes(subprocess.check_output(["git", "cat-file", "commit", "HEAD"]))
        (PRIVATE / "before").write_text(output("git", "status", "--porcelain", "--untracked-files=no"))
        bazel = ["bazel", f"--bazelrc={TEMP}/mss.bazelrc"]
        with (PRIVATE / "query.log").open("w") as log:
            selected = subprocess.run([*bazel, "query", "--output=label", "set(" + " ".join(LABELS) + ")"], text=True, stdout=subprocess.PIPE, stderr=log)
        result["query_exit"] = selected.returncode
        result["selected"] = selected.stdout.splitlines()
        assert selected.returncode == 0 and set(result["selected"]) == set(LABELS)
        args = ["test", "--config=x86_64", "--jobs=48", "--strategy=TestRunner=remote", "--nocache_test_results", "--keep_going", "--test_sharding_strategy=disabled", "--test_output=errors", "--test_timeout=120", "--test_env=GO_TEST_WRAP_TESTV=1", "--test_filter=^TestMSSSACKObservation$", "--build_event_json_file_path_conversion=false", f"--build_event_json_file={PRIVATE}/raw.bep", *LABELS]
        (PRIVATE / "args").write_text("\n".join(args))
        result["command_started_at"] = now()
        with (PRIVATE / "bazel.log").open("w") as log:
            completed = subprocess.run([*bazel, *args], stdout=log, stderr=subprocess.STDOUT)
        result["native_exit"] = completed.returncode
        result["command_finished_at"] = now()
        (PRIVATE / "exit").write_text(str(completed.returncode))
        (PRIVATE / "after").write_text(output("git", "status", "--porcelain", "--untracked-files=no"))
        (PRIVATE / "head").write_text(output("git", "rev-parse", "HEAD"))
        captured = report.capture(PRIVATE / "raw.bep", PRIVATE / "events.jsonl", Path(output(*bazel, "info", "output_base")), PRIVATE / "reports", expected_kind="test")
        (PRIVATE / "capture-exit").write_text(str(int(bool(captured["errors"]))))
        context = {"expected_source": source, "command_kind": "test", "execution_finished_at": result["command_finished_at"], **dict(zip(("commit_file", "source_before", "source_after", "final_head", "executed_arguments", "native_exit", "capture_exit"), ("commit", "before", "after", "head", "args", "exit", "capture-exit")))}
        write(PRIVATE / "context.json", context)
        (PRIVATE / "owners").write_text("\n".join(LABELS))
        verdict = report.verdict(PRIVATE / "events.jsonl", report.manifest_path(PRIVATE / "events.jsonl"), PRIVATE / "context.json", expected_targets=PRIVATE / "owners")
        result["qualification"] = verdict
        _, reports, errors = report.retained_reports(report.manifest_path(PRIVATE / "events.jsonl"))
        assert not errors
        result["observations"] = {}
        for row in verdict["executed"]:
            assert not row["cached_locally"] and row["execution_info"]["strategy"] == "remote" and not row["execution_info"].get("cachedRemotely", False)
        assert len(verdict["executed"]) == 2
        for (attempt, name), path in reports.items():
            arm = "native" if attempt.owner.label.endswith("_native_test") else "netstack"
            assert attempt.owner.label in LABELS and (attempt.run, attempt.shard, attempt.attempt) == (1, 1, 1)
            if name == "test.log":
                rows = [json.loads(raw) for raw in re.findall(r"MSSOBS (\{[^\n]*\})", path.read_text())]
                result["observations"][arm] = rows
                assert len(rows) == 3 and {row["case"] for row in rows} == CASES
                for row in rows:
                    assert row["send_bytes"] == 1460 and sum(packet["payload"] for packet in row["packets"]) == 1460
                    for packet in row["packets"]:
                        packet["options_hex"] = base64.b64decode(packet["options"] or "").hex()
                    row["gap_sack_on_data_observed"] = any(packet["sack_blocks"] for packet in row["packets"])
                    row["gap_ack_options_hex"] = base64.b64decode(row["gap_ack_options"] or "").hex()
                    row["syn_options_hex"] = base64.b64decode(row["syn_options"]).hex()
                    row["syn_ack_options_hex"] = base64.b64decode(row["syn_ack_options"]).hex()
            elif name == "test.xml":
                cases = list(ET.parse(path).getroot().iter("testcase"))
                expected = {"TestMSSSACKObservation", *("TestMSSSACKObservation/" + case for case in CASES)}
                assert len(cases) == 4 and {case.get("name") for case in cases} == expected
                assert all(case.find("failure") is None and case.find("error") is None and case.find("skipped") is None and case.get("status") != "notrun" and case.get("result") != "suppressed" for case in cases)
            else:
                continue
            (PUBLIC / f"{arm}-{name}").write_bytes(path.read_bytes())
        assert set(result["observations"]) == {"native", "netstack"}
        assert verdict["status"] == "PASS"
        result["status"] = "PASS"
    except Exception as error:
        result["error"] = {"type": type(error).__name__, "line": error.__traceback__.tb_lineno}
        if result.get("native_exit", 0) != 0 or result.get("qualification", {}).get("status") == "FAIL":
            result["status"] = "FAIL"
    result["verdict_at"] = now()
    write(PUBLIC / "result.json", result)
    return result["status"] == "PASS"


def scan():
    allowed = {"result.json", *(f"{arm}-test.{ext}" for arm in ("native", "netstack") for ext in ("log", "xml"))}
    key = os.environ["BUILDBUDDY_API_KEY"].encode()
    assert key
    for path in PUBLIC.iterdir():
        assert path.name in allowed and path.is_file() and not path.is_symlink()
        data = path.read_bytes()
        assert len(data) < 2_000_000 and key not in data and b"x-buildbuddy-api-key" not in data
    return True


if __name__ == "__main__":
    sys.exit(0 if (scan() if sys.argv[1] == "scan" else run()) else 1)
