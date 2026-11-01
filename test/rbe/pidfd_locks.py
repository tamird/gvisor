"""Qualify PIDFD lock preconditions and their existing syscall consumers."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import traceback
import uuid
import xml.etree.ElementTree as ET

from image_execution import execution_records
import qualification_report as reporter


ROOT = Path(os.environ["RUNNER_TEMP"])
PRIVATE = ROOT / "pidfd-lock-private"
PUBLIC = ROOT / "pidfd-lock-evidence"
FIXED = os.environ["FIXED"]
NEGATIVE = os.environ["NEGATIVE"]
KERNEL = "//pkg/sentry/kernel:kernel_nogo"
ANALYZERS = [KERNEL, "//pkg/sentry/kernel:kernel_test_nogo"]
SYSCALLS = ["//test/syscalls:pidfd_test_native", "//test/syscalls:pidfd_test_runsc_systrap"]
CASES = ["OpenPidfdSelfSucceeds", "Poll", "Epoll", "PidfdThreadLeaderDelayedNotification",
         "PidfdThreadSubthreadNotification", "PidfdThreadExecRevival"]
METHODS = ["pidStructFromTIDLocked", "notifyPIDFDsLocked", "notifyPIDFDsOnDeathLocked"]
PROPERTIES = {"Arch": "amd64", "OSFamily": "linux", "EstimatedMemory": "2GB",
              "workload-isolation-type": "firecracker", "container-image": "docker://" + os.environ["GNU"]}
PRIVATE.mkdir(exist_ok=True)
PUBLIC.mkdir(exist_ok=True)


def now():
    return datetime.now(timezone.utc).isoformat()


def write(result):
    (PUBLIC / "verdict.json").write_text(json.dumps(result, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", "-c", "safe.directory=" + str(Path.cwd()), *args])


def phase(result, name, source, labels, extra):
    out = PRIVATE / name
    out.mkdir(exist_ok=True)
    item = {"status": "INCOMPLETE", "verified": False, "source": source, "expected": labels,
            "invocation": str(uuid.uuid4()), "started": now()}
    result["phases"][name] = item
    write(result)
    git("checkout", "--quiet", "--detach", source)
    assert git("rev-parse", "HEAD").decode().strip() == source
    assert not git("diff", "HEAD", "--")
    (out / "commit").write_bytes(git("cat-file", "commit", source))
    (out / "before").write_bytes(git("diff", "HEAD", "--"))
    args = ["test", "--invocation_id=" + item["invocation"], "--config=x86_64", "--jobs=42",
            "--build_tag_filters=", "--test_tag_filters=", "--nocache_test_results",
            "--modify_execution_info=^TestRunner$=+no-remote-cache", "--test_timeout=300",
            "--test_sharding_strategy=disabled", "--test_output=errors", "--strategy=TestRunner=remote",
            "--remote_download_outputs=toplevel", "--remote_download_regex=.*[/]test[.](log|xml)$",
            "--build_event_json_file_path_conversion=false", "--build_event_json_file=" + str(out / "raw.json"),
            "--execution_log_json_file=" + str(out / "spawns.json"), *extra, *labels]
    (out / "arguments").write_text("\n".join(args) + "\n")
    with (out / "bazel.log").open("wb") as log:
        code = subprocess.run(["timeout", "--signal=INT", "--kill-after=30s", "25m", "bazel",
                               "--noblock_for_lock", "--bazelrc=" + str(ROOT / "gnu.bazelrc"), *args],
                              stdout=log, stderr=log).returncode
    item.update(native_exit=code, process_finished=now(), status="FAIL" if code else "INCOMPLETE")
    write(result)
    (out / "exit").write_text(str(code))
    (out / "after").write_bytes(git("diff", "HEAD", "--"))
    (out / "head").write_bytes(git("rev-parse", "HEAD"))
    captured = reporter.capture(out / "raw.json", out / "bep.json", Path("/"), out / "reports")
    (out / "capture-exit").write_text(str(int(bool(captured["errors"]))))
    context = {"expected_source": source, "commit_file": "commit", "source_before": "before",
               "source_after": "after", "final_head": "head", "executed_arguments": "arguments",
               "native_exit": "exit", "capture_exit": "capture-exit", "command_kind": "test",
               "execution_finished_at": item["process_finished"]}
    (out / "context.json").write_text(json.dumps(context))
    (out / "expected.txt").write_text("\n".join(labels) + "\n")
    verdict = reporter.verdict(out / "bep.json", reporter.manifest_path(out / "bep.json"),
                               out / "context.json", out / "expected.txt")
    item["qualification"] = {key: verdict[key] for key in
                             ("status", "case_counts", "errors", "missing", "bep_finish")}
    if verdict["status"] == "FAIL":
        item["status"] = "FAIL"
    write(result)
    assert not verdict["errors"] and not verdict["missing"]
    tests = []
    item["test_actions"] = tests
    with (out / "spawns.json").open() as stream:
        for _, action in execution_records(stream):
            if action is None or action.get("targetLabel") not in labels or action.get("mnemonic") != "TestRunner":
                continue
            args = action["commandArgs"]
            executable = Path(args[0]).name
            assert executable in ("test-setup.sh", "generate-xml.sh")
            props = {entry["name"]: entry["value"] for entry in action["platform"]["properties"]}
            row = {key: action.get(key) for key in ("targetLabel", "runner", "cacheHit", "digest")}
            row.update(executable=executable, exit=action.get("exitCode", 0), status=action.get("status", ""),
                       properties={key: props.get(key) for key in PROPERTIES})
            tests.append(row)
            write(result)
            assert row["runner"] == "remote" and not row["cacheHit"]
            assert row["properties"] == PROPERTIES
            if executable == "test-setup.sh":
                assert Path(args[1]).name == row["targetLabel"].split(":")[1]
    primary = [row for row in tests if row["executable"] == "test-setup.sh"]
    auxiliary = [row for row in tests if row["executable"] == "generate-xml.sh"]
    assert len(primary) == len(labels) and {row["targetLabel"] for row in primary} == set(labels)
    assert len(auxiliary) == len({row["targetLabel"] for row in auxiliary})
    if name == "negative":
        assert code == 3 and verdict["status"] == "FAIL"
        assert all(row["exit"] == 1 for row in primary)
    else:
        assert code == 0 and verdict["status"] == "PASS"
        assert all(row["exit"] == 0 and not row["status"] for row in tests)
    if name != "syscalls":
        observations = {}
        item["diagnostics"] = observations
        for label in labels:
            logs = [r for r in captured["reports"] if r["name"] == "test.log" and r["identity"]["label"] == label]
            assert len(logs) == 1
            text = (out / logs[0]["path"]).read_text()
            parts = re.split(r"(?m)^(amd64|arm64)\.\.\.", text)
            sections = {arch: body.strip().splitlines() for arch, body in zip(parts[1::2], parts[2::2])}
            assert len(parts) == 5 and set(sections) == {"amd64", "arm64"}
            observations[label] = {}
            for arch, lines in sections.items():
                if name == "negative":
                    matched = [re.fullmatch(r"checklocks: pkg/sentry/kernel/pidfd\.go:(\d+):\d+: must hold "
                                           r"t\.tg\.pidns\.owner\.mu exclusively \(.+\) to call (\w+), but not held "
                                           r"\(locks: .*\) \(GOOARCH=" + arch + r", GOOS=linux\)", line) for line in lines]
                    assert len(matched) == 3 and all(matched)
                    assert {m[2] for m in matched} == set(METHODS)
                    source_lines = Path("pkg/sentry/kernel/pidfd.go").read_text().splitlines()
                    expected = {method: next(i + 1 for i, line in reversed(list(enumerate(source_lines)))
                                             if "t." + method + "(" in line) for method in METHODS}
                    assert all(int(m[1]) == expected[m[2]] for m in matched)
                    observations[label][arch] = [{"line": int(m[1]), "method": m[2]} for m in matched]
                else:
                    assert lines == ["PASS"]
                    observations[label][arch] = "PASS"
    else:
        cases = {}
        item["cases"] = cases
        for label in labels:
            reports = [r for r in captured["reports"] if r["name"] == "test.xml" and r["identity"]["label"] == label]
            assert len(reports) == 1
            data = (out / reports[0]["path"]).read_bytes()
            entries = []
            for case in ET.fromstring(data).iter("testcase"):
                skipped = case.find("skipped") is not None or case.get("status") == "notrun" or case.get("result") == "suppressed"
                failed = case.find("failure") is not None or case.find("error") is not None
                entries.append({"class": case.get("classname"), "name": case.get("name"),
                                "status": "FAIL" if failed else "SKIP" if skipped else "PASS"})
            cases[label] = {"xml_sha256": hashlib.sha256(data).hexdigest(), "cases": entries}
            write(result)
            assert len(entries) == len(CASES) and {row["name"] for row in entries} == set(CASES)
            assert all(row["class"] == "PidfdTest" for row in entries)
            assert all(row["status"] == "PASS" or row["status"] == "SKIP" and label.endswith("_native")
                       and row["name"].startswith("PidfdThread") for row in entries)
    item.update(status=verdict["status"], verified=True)


if __name__ == "__main__":
    result = {"status": "INCOMPLETE", "phases": {}, "profile": "0ee70efe6d52576b8f173b40ad6b7ac117da3b72",
              "profile_files": {name: hashlib.sha256((ROOT / "gnu-platform" / name).read_bytes()).hexdigest()
                                for name in ("BUILD.bazel", "MODULE.bazel")}}
    write(result)
    for name, source, labels, extra in [
        ("negative", NEGATIVE, [KERNEL], []),
        ("analyzers", FIXED, ANALYZERS, []),
        ("syscalls", FIXED, SYSCALLS, ["--test_env=GTEST_FILTER=" + ":".join("PidfdTest." + case for case in CASES)]),
    ]:
        try:
            phase(result, name, source, labels, extra)
        except Exception as err:
            result["phases"][name]["collection_error"] = {"type": type(err).__name__, "line": traceback.extract_tb(err.__traceback__)[-1].lineno}
        result["phases"][name]["verified_at"] = now()
        write(result)
    status = "PASS" if all(item["verified"] for item in result["phases"].values()) else "INCOMPLETE"
    if any(item["status"] == "FAIL" for name, item in result["phases"].items() if name != "negative"):
        status = "FAIL"
    result.update(status=status, verified_at=now())
    write(result)
    raise SystemExit(result["status"] != "PASS")
