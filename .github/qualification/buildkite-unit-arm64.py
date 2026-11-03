"""Attribute the native integration catalog results to the pinned scanner and notices."""

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
OWNER = "//tools/licensecheck:notice_ranges_test"
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
    for name, blob in (("qemu", "8095135d50d970174ab0167816e259b677f4430a"), ("ltp", "d159169d1050894d3ea3b98e1c965c4058208fe1")):
        path = f"tools/licensecheck/testdata/qualification-{name}.txt"
        assert git("hash-object", path).decode().strip() == blob
    baseline = os.environ["BASELINE"]
    assert git("rev-parse", "HEAD^").decode().strip() == baseline
    assert git("diff", "--name-only", baseline, SOURCE).decode().splitlines() == [
        "tools/licensecheck/BUILD", "tools/licensecheck/notice_ranges_test.go",
        "tools/licensecheck/testdata/qualification-ltp.txt", "tools/licensecheck/testdata/qualification-qemu.txt"]
    RESULT["integration"] = baseline
    args = ["test", "--test_env=GO_TEST_WRAP_TESTV=1", "--nocache_test_results",
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
    cases = {c.get("name"): c for c in ET.parse(OUT / "test.xml").iter("testcase")}
    log = (OUT / "test.log").read_text(errors="replace")
    RESULT["diagnostic_lines"] = [
        line.strip()[:350] for line in log.splitlines()
        if "MATCH " in line or "before appendix =" in line or "full notice =" in line
    ]
    save()
    for name in ("qemu", "ltp"):
        case = cases["TestNoticeRanges/" + name]
        assert case.get("status") != "notrun" and case.get("result") != "suppressed"
        assert case.find("skipped") is None and case.find("error") is None
        assert case.find("failure") is None, name
        assert any(f"MATCH {name} " in line for line in RESULT["diagnostic_lines"])
    attempt, = verdict["executed"]
    assert attempt["owner"]["label"] == OWNER and all(attempt[key] == 1 for key in ("run", "shard", "attempt")), attempt
    assert not attempt["cached_locally"] and not attempt["execution_info"].get("cachedRemotely"), attempt
    assert attempt["execution_info"].get("strategy") in ("local", "processwrapper-sandbox", "linux-sandbox", "sandboxed"), attempt
    assert native_exit == 0 and verdict["status"] == "PASS", verdict
    assert not verdict["missing"] and not verdict["errors"] and not verdict["zero_case_owners"], verdict
    assert verdict["case_counts"] == {"passed": 3, "failed": 0, "skipped": 0}
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
