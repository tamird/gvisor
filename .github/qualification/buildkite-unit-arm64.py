"""Run the existing Buildkite unit selection on one native ARM64 host."""
from dataclasses import asdict
import datetime
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys

import qualification_report as report
import unit_matrix


TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "unit-results"
SOURCE = os.environ["PRODUCT"]


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args])


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def host() -> dict:
    proc = Path("/proc/self/status").read_text().splitlines()
    mounts = [line for line in Path("/proc/self/mountinfo").read_text().splitlines()
              if " - cgroup" in line]
    cgroup_v2 = Path("/sys/fs/cgroup/cgroup.controllers").is_file()
    value = dict(machine=platform.machine(), kernel=platform.release(), uid=os.getuid(),
                 gid=os.getgid(), cpu_count=os.cpu_count(), affinity=sorted(os.sched_getaffinity(0)),
                 meminfo=Path("/proc/meminfo").read_text().splitlines()[:3],
                 os_release=Path("/etc/os-release").read_text(), cgroup_mounts=mounts,
                 membership=Path("/proc/self/cgroup").read_text(), cgroup_v2=cgroup_v2,
                 restrictions=[line for line in proc if line.startswith(("Cap", "NoNewPrivs:", "Seccomp:"))])
    value["cgroup_limits"] = {name: path.read_text().strip() for name in ("cpu.max", "memory.max", "cpuset.cpus.effective")
                              if (path := Path("/sys/fs/cgroup") / name).is_file()}
    for name in ("apparmor_restrict_unprivileged_userns", "unprivileged_userns_clone"):
        path = Path("/proc/sys/kernel") / name
        value[name] = path.read_text().strip() if path.exists() else None
    report.write_json(OUT / "host.json", value)
    assert value["machine"] == "aarch64" and value["uid"] != 0, value
    assert platform.system() == "Linux" and mounts
    assert os.environ["CGROUPV2"] == str(cgroup_v2).lower()
    return value


def run(name: str, *, analysis: bool) -> tuple[Path, int]:
    directory = OUT / name
    directory.mkdir()
    raw = TMP / (name + ".jsonl")
    command = ["test", "--config=buildkite-unit", "--test_env=GO_TEST_WRAP_TESTV=true",
               "--nocache_test_results", "--flaky_test_attempts=1", "--runs_per_test=1",
               f"--build_event_json_file={raw}"]
    if analysis:
        command.append("--nobuild")
    (directory / "executed_arguments").write_text("\n".join(command) + "\n")
    (directory / "commit_file").write_bytes(git("cat-file", "commit", SOURCE))
    (directory / "source_before").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    (directory / "started_at").write_text(now())
    with (TMP / (name + ".console")).open("w") as console:
        completed = subprocess.run(["timeout", "--signal=TERM", "--kill-after=30s", "15m" if analysis else "90m", "bazel",
                                    f"--bazelrc={TMP / 'unit-resources.bazelrc'}", *command],
                                   stdout=console, stderr=subprocess.STDOUT)
    (directory / "execution_finished_at").write_text(now())
    (directory / "native_exit").write_text(str(completed.returncode))
    if completed.returncode:
        lines = (TMP / (name + ".console")).read_text(errors="replace").splitlines()
        errors = [index for index, line in enumerate(lines) if re.search(r"ERROR:|error:|FAILED|fatal error:", line)]
        indices = {index for start in errors[:3] for index in range(max(0, start - 1), min(len(lines), start + 10))}
        indices.update(range(max(0, len(lines) - 20), len(lines)))
        (directory / "diagnostic.txt").write_text("\n".join(lines[index] for index in sorted(indices))[:32768] + "\n")
    (directory / "source_after").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    (directory / "final_head").write_bytes(git("rev-parse", "HEAD"))
    # Keep the actual exit even if report collection fails.
    capture = report.capture(raw, directory / "events.jsonl", Path("bazel-out"), directory / "reports")
    (directory / "capture_exit").write_text("1" if capture["errors"] else "0")
    names = ("executed_arguments", "commit_file", "source_before", "source_after", "native_exit", "capture_exit", "final_head")
    context = {key: key for key in names}
    context.update(expected_source=SOURCE, command_kind="analysis" if analysis else "test",
                   execution_finished_at=(directory / "execution_finished_at").read_text())
    report.write_json(directory / "context.json", context)
    return directory, completed.returncode


def roots(events: Path) -> tuple[set[tuple], set[tuple]]:
    selected, skipped = set(), set()
    for line in events.read_text().splitlines():
        event = json.loads(line)
        if "configured" in event and event["configured"].get("targetKind") != "test_suite rule":
            for child in event.get("children", []):
                if owner := child.get("targetCompleted"):
                    selected.add((owner["label"], owner["configuration"]["id"], owner.get("aspect", "")))
        if event.get("aborted", {}).get("reason") == "SKIPPED" and (owner := event["id"].get("targetCompleted")):
            skipped.add((owner["label"], owner["configuration"]["id"], owner.get("aspect", "")))
    return selected - skipped, skipped


def main() -> None:
    receipt = dict(source=SOURCE, scope="native ARM64 full unit selection; actual host cgroup only",
                   status="INCOMPLETE", errors=[], started_at=now())
    report.write_json(OUT / "verdict.json", receipt)
    assert git("rev-parse", "HEAD").decode().strip() == SOURCE
    receipt["host"] = host()
    targets = OUT / "expected-targets.txt"
    preflight = None
    try:
        preflight, code = run("analysis", analysis=True)
        selected = unit_matrix.configured_targets(str(TMP / "analysis.jsonl"), tests=True,
                                                 allow_test_no_build=True, expected_exit=code)
        targets.write_text("\n".join(sorted(selected)) + "\n")
        report.write_json(preflight / "selected.json", {key: asdict(value) for key, value in sorted(selected.items())})
        receipt["selected_tests"] = len(selected)
        configurations = {event["id"]["configuration"]["id"]: event["configuration"]
                          for line in (TMP / "analysis.jsonl").read_text().splitlines()
                          if "configuration" in (event := json.loads(line)) and "configuration" in event["id"]}
        selected_configurations = {target.configuration: configurations[target.configuration] for target in selected.values()}
        report.write_json(preflight / "configurations.json", selected_configurations)
        assert all(config["cpu"] == "aarch64" for config in selected_configurations.values()), selected_configurations
        assert len(selected) == 355, len(selected)
    except Exception as error:
        receipt["errors"].append(f"analysis: {error!r}")
    # A projection error must not suppress the independent full execution.
    try:
        directory, code = run("full", analysis=False)
        receipt["native_exit"] = code
        events = directory / "events.jsonl"
        verdict = report.verdict(events, report.manifest_path(events), directory / "context.json",
                                 targets if targets.exists() else None, ())
        report.write_json(directory / "verdict.json", verdict)
        receipt["suite_status"] = verdict["status"]
        receipt["case_counts"] = verdict["case_counts"]
        receipt["zero_case_owners"] = verdict["zero_case_owners"]
        receipt["all_skipped_owners"] = verdict["all_skipped_owners"]
        receipt["coverage_gaps"] = {key: verdict[key] for key in ("zero_case_owners", "all_skipped_owners")}
        receipt["coverage_status"] = "INCOMPLETE" if any(receipt["coverage_gaps"].values()) else "COMPLETE"
        receipt["skips"] = verdict["skips"]
        receipt["missing"] = verdict["missing"]
        receipt["test_attempts"] = len(verdict["executed"])
        receipt["invalid_attempts"] = [row for row in verdict["executed"] if
                                       row["cached_locally"] or row["attempt"] != 1 or row["run"] != 1 or
                                       row["execution_info"].get("cachedRemotely") or
                                       row["execution_info"].get("strategy") not in ("local", "linux-sandbox", "processwrapper-sandbox")]
        assert preflight is not None
        expected, skipped = roots(preflight / "events.jsonl")
        actual, actual_skipped = roots(events)
        runnable = set(targets.read_text().splitlines())
        auxiliary = {owner for owner in expected if owner[2] or owner[0] not in runnable}
        receipt["auxiliary_roots"] = len(auxiliary)
        assert auxiliary and len(expected) > len(runnable) == 355, (len(expected), len(runnable), len(auxiliary))
        assert (actual, actual_skipped) == (expected, skipped), "Configured root or skipped sets changed"
        completed = {}
        for line in events.read_text().splitlines():
            event = json.loads(line)
            if "completed" in event:
                owner = event["id"]["targetCompleted"]
                completed[(owner["label"], owner["configuration"]["id"], owner.get("aspect", ""))] = event["completed"].get("success")
        absent = sorted(expected - completed.keys())
        failed = sorted(owner for owner in expected if owner in completed and not completed[owner])
        receipt.update(configured_roots=len(expected), skipped_roots=sorted(skipped),
                       missing_build_roots=absent, failed_build_roots=failed)
        report.write_json(directory / "build-roots.json", dict(expected=sorted(expected), completed=sorted(completed.items())))
        assert not absent and not failed, (absent, failed)
        assert not receipt["invalid_attempts"], receipt["invalid_attempts"]
        assert verdict["status"] == "PASS", (verdict["errors"], verdict["failures"], verdict["missing"])
    except Exception as error:
        receipt["errors"].append(f"full: {error!r}")
    receipt["status"] = ("FAIL" if receipt.get("native_exit", 0) != 0 or receipt.get("suite_status") == "FAIL"
                         else "INCOMPLETE" if receipt["errors"] or receipt.get("coverage_status") != "COMPLETE" else "PASS")
    receipt["verdict_available_at"] = now()
    report.write_json(OUT / "verdict.json", receipt)
    print(json.dumps({key: receipt.get(key) for key in ("status", "selected_tests", "test_attempts", "case_counts", "coverage_gaps", "errors")}))
    if receipt["status"] != "PASS":
        sys.exit(1)


if __name__ == "__main__":
    main()
