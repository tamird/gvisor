"""Run the existing Buildkite unit selection on one native ARM64 host."""
import ast
from collections import Counter
import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import zipfile

import qualification_report as report
from stdlib_provenance import fields, first, number


TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "unit-results"
SOURCE = os.environ["PRODUCT"]
SELECTED = {
    "//examples/sandboxexec/go/docprocessor:docprocessor_test",
    "//runsc/cmd/sentry/sentrycmd:sentrycmd_test",
    "//runsc/cmd:cmd_test",
    "//sandboxexec/sandbox/python:sandbox_py_test",
    "//sandboxexec/sandbox:sandbox_test",
    "//test/trace:trace_test",
}
POLICY_KEYS = ("machine", "kernel", "uid", "gid", "os_release", "cgroup_v2", "restrictions",
               "apparmor_restrict_unprivileged_userns", "unprivileged_userns_clone")


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args])


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def host(report_name: str) -> dict:
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
    report.write_json(OUT / report_name, value)
    assert value["machine"] == "aarch64" and value["uid"] != 0, value
    assert platform.system() == "Linux" and mounts
    assert 'VERSION_ID="22.04"' in value["os_release"] and 'ID=ubuntu' in value["os_release"], value["os_release"]
    assert os.environ["CGROUPV2"] == str(cgroup_v2).lower()
    return value


def run(name: str, targets: Path) -> tuple[Path, int]:
    directory = OUT / name
    directory.mkdir()
    raw = TMP / (name + ".jsonl")
    command = ["test", "--config=buildkite-unit", "--test_env=GO_TEST_WRAP_TESTV=true",
               "--nocache_test_results", "--flaky_test_attempts=1", "--runs_per_test=1",
               f"--target_pattern_file={targets}", f"--build_event_json_file={raw}",
               f"--execution_log_compact_file={TMP / (name + '.compact')}"]
    (directory / "executed_arguments").write_text("\n".join(command) + "\n")
    (directory / "commit_file").write_bytes(git("cat-file", "commit", SOURCE))
    (directory / "source_before").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    (directory / "started_at").write_text(now())
    with (TMP / (name + ".console")).open("w") as console:
        completed = subprocess.run(["timeout", "--signal=TERM", "--kill-after=30s", "25m", "bazel",
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
    context.update(expected_source=SOURCE, command_kind="test",
                   execution_finished_at=(directory / "execution_finished_at").read_text())
    report.write_json(directory / "context.json", context)
    return directory, completed.returncode


def originals() -> list[dict]:
    rows = []
    for name, digest, member, run_id, exit_code in [
        ("original.zip", "07c35476420c24269fdf87f792e6dfdafcbbb3ce5f452cb3d20cfdf2b9a84b10",
         "arm64-execution.json", 41513096230, 124),
        ("remaining.zip", "0c2fbb08e99de4db89ec49e9b60355b977a6cfb91bb7f12c33b7e80570922df9",
         "full/verdict.json", 41589548481, 1),
    ]:
        path = TMP / name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, name
        with zipfile.ZipFile(path) as archive:
            previous = json.loads(archive.read(member))
            if name == "original.zip":
                analysis = json.loads(archive.read("arm64-analysis-selection.json"))
                eligible = set(analysis["runnable_tests"])
                assert len(eligible) == 355 and SELECTED <= eligible, sorted(eligible)
        assert previous["source"] == SOURCE and previous["native_exit"] == exit_code and previous["status"] == "FAIL"
        selected = [row for row in previous["executed"] if row["owner"]["label"] in SELECTED]
        assert all(row["status"] == "FAILED" for row in selected), selected
        rows.extend(dict(source_run=run_id, zip_sha256=digest, native_exit=exit_code,
                         owner=row["owner"], status=row["status"], xml=row["xml"]) for row in selected)
    assert len(rows) == 6 and {r["owner"]["label"] for r in rows} == SELECTED, rows
    return rows


def python_cases(directory: Path) -> dict:
    source = Path("sandboxexec/sandbox/python/tests/sandbox_test.py").read_bytes()
    methods = sorted(n.name for n in ast.walk(ast.parse(source))
                     if isinstance(n, ast.FunctionDef) and n.name.startswith("test_"))
    assert len(methods) == len(set(methods)) == 39, methods
    path, = directory.glob("reports/*/testlogs/sandboxexec/sandbox/python/sandbox_py_test/test.log")
    text = path.read_text()
    counts = re.findall(r"^Ran ([0-9]+) tests? in ", text, re.M)
    statuses = re.findall(r"^(OK(?: \(.*\))?|FAILED \(.*\))$", text, re.M)
    return dict(source_sha256=hashlib.sha256(source).hexdigest(), expected_methods=methods,
                log_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                observed_counts=counts, terminal_statuses=statuses,
                complete=counts == [str(len(methods))] and statuses == ["OK"])


def compilation(path: Path) -> dict:
    counts = Counter()
    examples = []
    with subprocess.Popen(["zstd", "-qdc", path], stdout=subprocess.PIPE) as proc:
        while (n := number(proc.stdout)) is not None:
            entry = fields(proc.stdout.read(n))
            if 7 not in entry:
                continue
            spawn = fields(first(entry, 7))
            if first(spawn, 8) != b"GoCompilePkg":
                continue
            runner = first(spawn, 11).decode()
            cached = bool(first(spawn, 12, 0))
            succeeded = not first(spawn, 10) and first(spawn, 9, 0) == 0
            counts[(runner, cached, succeeded)] += 1
            if runner == "remote" and not cached and succeeded and len(examples) < 10:
                examples.append(first(spawn, 7).decode())
        assert proc.wait() == 0, "Incomplete execution log"
    return dict(runners=[dict(strategy=r, cached=c, succeeded=s, count=n)
                         for (r, c, s), n in sorted(counts.items())], fresh_remote_examples=examples)


def main() -> None:
    receipt = dict(source=SOURCE, scope="Six original permission-failed ARM64 unit owners on default Ubuntu 22.04",
                   source_run=41513096230, source_artifact=13175395076, status="INCOMPLETE", errors=[], started_at=now())
    report.write_json(OUT / "verdict.json", receipt)
    try:
        assert git("rev-parse", "HEAD").decode().strip() == SOURCE
        before = host("host.json")
        receipt["original_failures"] = originals()
        probe = subprocess.run(["unshare", "-Ur", "true"], capture_output=True, text=True)
        receipt["namespace_probe"] = dict(exit=probe.returncode, stderr=probe.stderr[:1000])
        report.write_json(OUT / "verdict.json", receipt)
        assert before["apparmor_restrict_unprivileged_userns"] in (None, "0"), "Default worker restricts user namespaces"
        assert probe.returncode == 0, "Default worker cannot create the required user namespace"
        selected = SELECTED
        targets = OUT / "expected-targets.txt"
        targets.write_text("\n".join(sorted(selected)) + "\n")
        receipt.update(selected=len(selected), previous_run=41589548481, previous_artifact=13212422877)
        directory, code = run("full", targets)
        events = directory / "events.jsonl"
        v = report.verdict(events, report.manifest_path(events), directory / "context.json", targets, (),
                           test_tag_filters=("-nogo", "-requires-kvm"))
        report.write_json(directory / "verdict.json", v)
        rows = v["executed"]
        attempted = {row["owner"]["label"] for row in rows}
        missing = selected - attempted
        invalid = [row["owner"] for row in rows if row["cached_locally"] or row["attempt"] != 1 or row["run"] != 1
                   or row["execution_info"].get("cachedRemotely")
                   or row["execution_info"].get("strategy") not in ("local", "linux-sandbox", "processwrapper-sandbox")]
        configs = {}
        configured = set()
        with (TMP / "full.jsonl").open() as raw:
            for line in raw:
                e = json.loads(line)
                if "configuration" in e and "configuration" in e["id"]:
                    configs[e["id"]["configuration"]["id"]] = e["configuration"].get("cpu")
                if "configured" in e:
                    configured.update(c["targetCompleted"]["label"] for c in e.get("children", []) if "targetCompleted" in c)
        cpus = {row["configuration"]: configs.get(row["configuration"]) for row in v["expected"]}
        receipt.update(attempted=len(attempted), remaining_unattempted=sorted(missing), unexpected=sorted(attempted - selected),
                       configured_labels=len(configured), unexpected_configured=sorted(configured - selected),
                       cpu_by_configuration=cpus, invalid_attempts=invalid, suite_status=v["status"],
                       case_counts=v["case_counts"], skips=v["skips"], zero=v["zero_case_owners"], all_skipped=v["all_skipped_owners"],
                       failed=v["failures"], native_exit=code)
        if invalid or selected != configured or not cpus or set(cpus.values()) != {"aarch64"} or attempted - selected:
            receipt["errors"].append("Source selection or native test placement disagrees; see detailed evidence")
        # Preserve the primary owner verdict before optional execution-log decoding.
        report.write_json(OUT / "verdict.json", receipt)
        after = host("host-after.json")
        receipt["host_policy_unchanged"] = all(before[key] == after[key] for key in POLICY_KEYS)
        if not receipt["host_policy_unchanged"]:
            receipt["errors"].append("Host policy changed during the test invocation")
        receipt["python_unittest"] = python_cases(directory)
        if not receipt["python_unittest"]["complete"]:
            receipt["errors"].append("Python unittest census is incomplete or reports a failure/skip")
        receipt["compiler"] = compilation(TMP / "full.compact")
    except Exception as error:
        receipt["errors"].append(str(error)[:2000])
    directory = OUT / "full"
    if (directory / "native_exit").is_file():
        receipt["native_exit"] = int((directory / "native_exit").read_text())
        receipt["execution_finished_at"] = (directory / "execution_finished_at").read_text()
    if receipt.get("native_exit", 0) or receipt.get("suite_status") == "FAIL":
        receipt["status"] = "FAIL"
    elif (receipt.get("suite_status") == "PASS" and not receipt["errors"] and not receipt["remaining_unattempted"]
          and not receipt["zero"] and not receipt["all_skipped"] and receipt.get("host_policy_unchanged")):
        # Individual skips remain explicit; successful owner completion is not a claim that every case ran.
        receipt["status"] = "PASS"
    receipt["verdict_available_at"] = now()
    report.write_json(OUT / "verdict.json", receipt)
    print(json.dumps({k: receipt.get(k) for k in ("status", "native_exit", "attempted", "case_counts", "errors")}))
    sys.exit(receipt["status"] != "PASS")


if __name__ == "__main__":
    main()
