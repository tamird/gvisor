"""Join the CLI printer and restored PID to the same native checkpoint."""

import json
import os
from pathlib import Path
import platform
import re
import runpy
import subprocess
from urllib.parse import unquote, urlparse
import xml.etree.ElementTree as ET


WORK = Path(os.environ["RUNNER_TEMP"]) / "cmd-guest-fixture"
HARNESS = WORK / "cmd_guest.py"
ORIGINAL = runpy.run_path(str(HARNESS))
ALLOCATOR = "//pkg/sentry/pgalloc:pgalloc_test"
CMD = ORIGINAL["OWNER"]
CASE = ORIGINAL["CHECKPOINT"]


def trial(owner, case, index, results):
    row = {"owner": owner, "case": case, "qualified": False}
    results.append(row)
    phase = ("alloc" if owner == ALLOCATOR else "guest") + str(index)
    bep = WORK / (phase + ".bep")
    pattern = "/".join("^" + re.escape(n) + "$" for n in case.split("/"))
    args = [
        str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx3000m",
        "test", "--config=aarch64", "-c", "opt", "--strip=never", "--jobs=3", "--local_resources=memory=9900",
        "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
        "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox",
        "--remote_executor=", "--remote_cache=", "--bes_backend=", "--spawn_strategy=linux-sandbox",
        "--strategy=TestRunner=standalone", "--nocache_test_results", "--flaky_test_attempts=1",
        "--test_timeout=900", "--test_env=GO_TEST_WRAP_TESTV=1", "--test_env=CGROUPV2=true",
        "--test_output=errors", "--test_filter=" + pattern, "--test_arg=-test.run=" + pattern,
        "--build_event_json_file=" + str(bep), "--noshow_progress", "--color=no", "--curses=no",
    ]
    if owner == CMD:
        args.append("--run_under=/usr/bin/python3 -IB " + str(HARNESS) + " under")
    with (WORK / (phase + ".log")).open("w") as out:
        row["bazel_exit"] = subprocess.run([*args, owner], stdout=out, stderr=subprocess.STDOUT).returncode
    events = (json.loads(s) for s in bep.read_text().splitlines())
    record, = [e["testResult"] for e in events if "testResult" in e and e["id"].get("testResult", {}).get("label") == owner]
    row["status"] = record["status"]
    assert not record.get("cachedLocally") and record["executionInfo"]["strategy"] in {"standalone", "local"}
    files = {}
    for part in record["testActionOutput"]:
        name = part["name"]
        if name in {"test.log", "test.xml"}:
            uri = urlparse(part["uri"])
            path = Path(unquote(uri.path))
            assert uri.scheme == "file" and str(path).endswith("/" + owner[2:].replace(":", "/") + "/" + name)
            sha = ORIGINAL["digest"](path)
            assert not part.get("digest") or part["digest"] == sha
            row[name] = sha
            files[name] = path
    text = files["test.log"].read_text(errors="replace")
    cases = {}
    for el in ET.parse(files["test.xml"]).iter("testcase"):
        name = el.get("name").removeprefix("cmd.").removeprefix("pgalloc.")
        assert name not in cases
        cases[name] = "fail" if (el.find("failure") is not None or el.find("error") is not None) else (
            "skip" if el.find("skipped") is not None else "pass")
    row["cases"] = cases
    if row["bazel_exit"] or row["status"] != "PASSED":
        row["diagnostics"] = ORIGINAL["fail_text"](text)
    if owner == CMD:
        printed = re.search(r"CHECKPOINT_PRINT sha256=([0-9a-f]{64}) print=(pass|unreachable|other-error)\b", text)
        restored = re.search(r"CHECKPOINT_RESTORED sha256=([0-9a-f]{64}) pid=1 exe=test_app\b", text)
        if printed:
            row.update(checkpoint_sha256=printed[1], print_status=printed[2])
        row["restored_same_image"] = bool(printed and restored and printed[1] == restored[1])
        facts, = [json.loads(s.partition("CMD_GUEST_ORIGINAL ")[2]) for s in text.splitlines() if s.startswith("CMD_GUEST_ORIGINAL ")]
        row["native"] = facts
        assert facts["inner_exit"] == 0 and facts["uname"] == "aarch64" and facts["test_app"]["machine"] == 183
        assert row["restored_same_image"]
    else:
        found = re.search(r"MEMORYFILE_CONTROL restored=true metadata_bytes=(\d+) actual_end=(\d+) guessed_end=(\d+) framed_print=true original_matches=false original_print_exit_ok=(true|false)", text)
        assert found
        row.update(metadata_bytes=int(found[1]), actual_end=int(found[2]), generic_end=int(found[3]),
                   unmodified_printer_returned_success=found[4] == "true")
    assert set(cases) == {case, case.split("/")[0]} and all(s == "pass" for s in cases.values())
    assert row["bazel_exit"] == 0 and row["status"] == "PASSED" and "--- PASS: " + case in text
    row["qualified"] = True


report = {"kernel": platform.release(), "arch": platform.machine(), "trials": [], "qualified": False}
assert report["arch"] == "aarch64" and report["kernel"].startswith("6.8.")
receipt = WORK / "result.json"
for i, (owner, case) in enumerate([(ALLOCATOR, "TestSameSavedMemoryFilePrintAndLoad")] + [(CMD, CASE)] * 5):
    try:
        trial(owner, case, i, report["trials"])
    except (AssertionError, AttributeError, KeyError, OSError, ValueError) as err:
        row = report["trials"][-1]
        row["collector_error"] = type(err).__name__
        if not row.get("status"):
            phase = ("alloc" if owner == ALLOCATOR else "guest") + str(i)
            log = (WORK / (phase + ".log")).read_text(errors="replace")
            row["analysis_diagnostics"] = ORIGINAL["fail_text"](log)
    receipt.write_text(json.dumps(report, indent=2))
    if owner == CMD and (not report["trials"][-1]["qualified"] or report["trials"][-1].get("print_status") == "unreachable"):
        break
report["qualified"] = all(t["qualified"] for t in report["trials"]) and any(t.get("print_status") == "unreachable" for t in report["trials"])
receipt.write_text(json.dumps(report, indent=2))
raise SystemExit(0 if report["qualified"] else 1)
