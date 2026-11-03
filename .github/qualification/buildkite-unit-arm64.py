"""Exercise the unchanged license owner through Make's native ARM64 command."""

import datetime
import os
from pathlib import Path
import platform
import re
import subprocess
import xml.etree.ElementTree as ET

import qualification_report as report


TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "unit-results"
SOURCE = os.environ["PRODUCT"]
OWNER = "//tools/licensecheck:licensecheck_test"
RESULT: dict[str, object] = {"status": "INCOMPLETE", "source": SOURCE}


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args])


def save() -> None:
    report.write_json(OUT / "verdict.json", RESULT)


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def main() -> None:
    assert git("rev-parse", "HEAD").decode().strip() == SOURCE
    assert platform.machine() == "aarch64", platform.machine()
    assert os.getuid() != 0, "Expected the ordinary hosted runner user"
    cgroup = "true" if Path("/sys/fs/cgroup/cgroup.controllers").is_file() else "false"
    RESULT["host"] = {"architecture": platform.machine(), "kernel": platform.release(),
                      "uid": os.getuid(), "cpus": os.cpu_count(), "cgroup_v2": cgroup,
                      "memory": next(line for line in Path("/proc/meminfo").read_text().splitlines()
                                     if line.startswith("MemTotal:")),
                      "apparmor_restrict_unprivileged_userns": Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns").read_text().strip()}
    steps = []
    for file, count in (("pipeline", 3), ("first-pass", 1)):
        text = Path(f".buildkite/{file}.yaml").read_text()
        commands = re.findall(r'(?m)^    label: "[^"\n]*Unit tests [^"\n]*"\n    command: (.+)\n    env:\n      DOCKER_BUILD: "false"$', text)
        assert commands == ["make unit-tests"] * count, (file, commands)
        steps.extend(commands)
    unchanged = ["tools/licensecheck", "governance/licensing.yaml", "MODULE.bazel", "go.mod", "go.sum"]
    assert not git("diff", os.environ["BASELINE"], SOURCE, "--", *unchanged)
    RESULT["baseline"] = {"source": os.environ["BASELINE"], "unchanged_paths": unchanged,
                          "arm64_unit_job": "https://buildkite.com/gvisor/pipeline/builds/53249#01a19c21-d0a1-4e8b-8dca-646db0259dc6"}
    RESULT["buildkite_commands"] = steps
    recorder = TMP / "record-bazel"
    recorder.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$RUNNER_TEMP/make-arguments"\n')
    recorder.chmod(0o755)
    subprocess.run(["make", "-s", "unit-tests", "DOCKER_BUILD=false", f"BAZEL={recorder}"], check=True)
    original = (TMP / "make-arguments").read_text().splitlines()
    assert original[0] == "test", original
    separator = original.index("--")
    assert original[separator + 1:] == ["//:all", "pkg/...", "tools/...", "runsc/...", "vdso/...", "sandboxexec/...", "examples/...", "test/trace/..."], original
    assert f"--test_env=CGROUPV2={cgroup}" in original, original
    RESULT["original_make_arguments"] = original
    args = original[:separator] + ["--test_env=GO_TEST_WRAP_TESTV=1", "--nocache_test_results",
                                  "--flaky_test_attempts=1", "--runs_per_test=1",
                                  f"--build_event_json_file={TMP / 'raw.jsonl'}", "--", OWNER]
    directory = TMP / "owner"
    directory.mkdir()
    (directory / "executed_arguments").write_text("\n".join(args) + "\n")
    (directory / "commit_file").write_bytes(git("cat-file", "commit", SOURCE))
    (directory / "source_before").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    (TMP / "owners").write_text(OWNER + "\n")
    RESULT["started_at"] = now()
    save()
    with (TMP / "console").open("w") as console:
        try:
            result = subprocess.run(["bazel", f"--bazelrc={TMP / 'unit-resources.bazelrc'}", *args],
                                    stdout=console, stderr=subprocess.STDOUT, timeout=2400)
            native_exit = result.returncode
        except subprocess.TimeoutExpired:
            native_exit = 124
    RESULT.update(native_exit=native_exit, execution_finished_at=now())
    save()
    (directory / "native_exit").write_text(str(native_exit))
    (directory / "source_after").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    (directory / "final_head").write_bytes(git("rev-parse", "HEAD"))
    if native_exit:
        lines = (TMP / "console").read_text(errors="replace").splitlines()
        RESULT["diagnostics"] = [line[:600] for line in lines if re.match(r"^(ERROR:|FATAL:|FAIL:|FAIL\t)", line)][:20]
        save()
    events = directory / "events.jsonl"
    captured = report.capture(TMP / "raw.jsonl", events, Path("bazel-out"), directory / "reports")
    (directory / "capture_exit").write_text("1" if captured["errors"] else "0")
    keys = ("executed_arguments", "commit_file", "source_before", "source_after", "native_exit", "capture_exit", "final_head")
    context = {key: key for key in keys}
    context.update(expected_source=SOURCE, command_kind="test", execution_finished_at=RESULT["execution_finished_at"])
    report.write_json(directory / "context.json", context)
    verdict = report.verdict(events, report.manifest_path(events), directory / "context.json", TMP / "owners")
    RESULT["owner_verdict"] = verdict
    RESULT["capture"] = captured
    save()
    for ext in ("xml", "log"):
        source, = (directory / "reports").rglob("test." + ext)
        (OUT / ("test." + ext)).write_bytes(source.read_bytes())
    policy, = [case for case in ET.parse(OUT / "test.xml").iter("testcase") if case.get("name") == "TestLicensePolicy"]
    RESULT["policy_case"] = dict(policy.attrib)
    assert all(policy.find(child) is None for child in ("failure", "error", "skipped")), ET.tostring(policy).decode()
    assert policy.get("status") != "notrun" and policy.get("result") != "suppressed", policy.attrib
    attempt, = verdict["executed"]
    assert attempt["owner"]["label"] == OWNER and all(attempt[key] == 1 for key in ("run", "shard", "attempt")), attempt
    assert not attempt["cached_locally"] and not attempt["execution_info"].get("cachedRemotely"), attempt
    assert attempt["execution_info"].get("strategy") in ("local", "processwrapper-sandbox", "linux-sandbox", "sandboxed"), attempt
    assert verdict["status"] == "PASS" and not verdict["zero_case_owners"] and not verdict["all_skipped_owners"], verdict
    RESULT["status"] = "PASS"


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        RESULT["status"] = "FAIL" if RESULT.get("native_exit", 0) else "INCOMPLETE"
        RESULT["verification_error"] = f"{type(error).__name__}: {error}"
    RESULT["verdict_available_at"] = now()
    save()
    raise SystemExit(0 if RESULT["status"] == "PASS" else 1)
