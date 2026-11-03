"""Run the existing Buildkite unit selection on one native ARM64 host."""
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
import xml.etree.ElementTree as ET

import qualification_report as report
from stdlib_provenance import fields, first, number


TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "unit-results"
SOURCE = os.environ["PRODUCT"]
PRODUCTION = "96fadde722e14d482a62276b80237398f8b62fbf"
SELECTED = {"//runsc/cmd:cmd_test"}
CASE = "TestCheckpointStateEngineFlag/override_boot_wire_and_preserve_boot_default"
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
    raw = (TMP / "original.zip").read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == "12ceaeb70b255029cc63a3d69c6001bb4f87c1e200af68e201c628789595a929"
    with zipfile.ZipFile(TMP / "original.zip") as archive:
        previous = json.loads(archive.read("full/verdict.json"))
    assert previous["source"] == PRODUCTION and previous["native_exit"] == 3 and previous["status"] == "FAIL"
    row, = [r for r in previous["executed"] if r["owner"]["label"] in SELECTED]
    assert row["status"] == "FAILED" and row["xml"]["failed"] == 2
    return [dict(source_run=41608714830, zip_sha256=digest, native_exit=3,
                 owner=row["owner"], status=row["status"], failed_cases=row["xml"]["failed"])]


def child_diagnostics(directory: Path) -> dict:
    log, = directory.glob("reports/*/testlogs/runsc/cmd/cmd_test/test.log")
    xml, = directory.glob("reports/*/testlogs/runsc/cmd/cmd_test/test.xml")
    cases = list(ET.fromstring(xml.read_bytes()).iter("testcase"))
    names = sorted(case.attrib["name"] for case in cases)
    focused = [case for case in cases if case.attrib["name"] == CASE]
    focused_executed = len(focused) == 1 and focused[0].find("skipped") is None
    focused_failed = focused_executed and any(focused[0].find(tag) is not None for tag in ("failure", "error"))
    with zipfile.ZipFile(TMP / "original.zip") as archive:
        old_xml, = [name for name in archive.namelist() if name.endswith("/cmd_test/test.xml")]
        old_names = sorted(case.attrib["name"] for case in ET.fromstring(archive.read(old_xml)).iter("testcase"))
    names_sha = hashlib.sha256(("\n".join(names) + "\n").encode()).hexdigest()
    text = log.read_text()
    sites = [dict(case=case, file=file, line=int(line), valid=valid == "true")
             for case, file, line, valid in re.findall(r"STATEFILE_SITE case=(\S+) file=(\S+) line=([0-9]+) ok=(true|false)", text)]
    children = []
    pattern = (r"STATEFILE_CHILD case=(\S+) stage=(original|debug) exit=(-?[0-9]+) signal=(-?[0-9]+) "
               r"output_bytes=([0-9]+) output_sha256=([0-9a-f]{64})")
    for case, stage, code, signal, size, digest in re.findall(pattern, text):
        children.append(dict(case=case, stage=stage, exit=int(code), signal=int(signal),
                             output_bytes=int(size), output_sha256=digest))
    inputs = []
    pattern = (r"STATEFILE_INPUT case=(\S+) before_bytes=([0-9]+) before_sha256=([0-9a-f]{64}) "
               r"after_bytes=([0-9]+) after_sha256=([0-9a-f]{64})")
    for case, before_size, before_sha, after_size, after_sha in re.findall(pattern, text):
        inputs.append(dict(case=case, before_bytes=int(before_size), before_sha256=before_sha,
                           after_bytes=int(after_size), after_sha256=after_sha))
    debug_logs = []
    pattern = r"STATEFILE_DEBUG case=(\S+) readable=(true|false) bytes=([0-9]+) sha256=([0-9a-f]{64})"
    for case, readable, size, digest in re.findall(pattern, text):
        debug_logs.append(dict(case=case, readable=readable == "true", bytes=int(size), sha256=digest))
    diagnostics = []
    for line in text.splitlines():
        if "STATEFILE_ERROR " not in line:
            continue
        match = re.search(r"STATEFILE_ERROR case=(\S+) stage=(original|debug|debug_log) (.*)", line)
        assert match, "Malformed child diagnostic"
        case, stage, value = match.groups()
        assert value.startswith(("FATAL ERROR: ", "panic:", "fatal error:", "runtime:")), "Unknown child diagnostic"
        assert len(value) <= 1024, "Oversized child diagnostic"
        category = "unknown-fatal-diagnostic"
        prefixes = {
            "FATAL ERROR: error printing state: ": "state-print-error",
            "FATAL ERROR: error verifying statefile: ": "state-verification-error",
            "FATAL ERROR: error parsing statefile: ": "state-parse-error",
            "FATAL ERROR: error writing output: ": "output-write-error",
            "FATAL ERROR: error opening input: ": "input-open-error",
            "FATAL ERROR: error statting input: ": "input-stat-error",
            "panic: runtime error: index out of range": "index-bounds-panic",
            "panic: runtime error: slice bounds out of range": "slice-bounds-panic",
            "panic: runtime error: invalid memory address or nil pointer dereference": "nil-pointer-panic",
            "fatal error: concurrent map": "concurrent-map-fatal",
        }
        for prefix, known_category in prefixes.items():
            if value.startswith(prefix):
                category = known_category
                break
        row = dict(case=case, stage=stage, category=category, captured_bytes=len(value.encode()),
                   captured_sha256=hashlib.sha256(value.encode()).hexdigest(), source_bound_reached=len(value) == 1024)
        if row not in diagnostics:
            diagnostics.append(row)
    failed_cases = {row["case"] for row in children if row["stage"] == "original"}
    issues = []
    if not focused_executed:
        issues.append("The focused leaf is missing or skipped")
    if names != old_names:
        issues.append("Full cmd case census differs from the original")
    if len(children) > 16 or any(row["case"] not in names for row in [*children, *inputs, *debug_logs, *diagnostics, *sites]):
        issues.append("Unexpected child record")
    for case in failed_cases:
        originals = [r for r in children if r["case"] == case and r["stage"] == "original"]
        replays = [r for r in children if r["case"] == case and r["stage"] == "debug"]
        snapshots = [r for r in inputs if r["case"] == case]
        logs = [r for r in debug_logs if r["case"] == case]
        callers = [r for r in sites if r["case"] == case]
        if len(callers) != 1 or callers[0]["file"] != "statefile_test.go" or not callers[0]["valid"] or callers[0]["line"] <= 0:
            issues.append("Missing or unexpected printer callsite")
        if len(originals) != 1 or len(replays) != 1 or len(snapshots) != 1 or len(logs) != 1:
            issues.append("Incomplete same-checkpoint replay")
            continue
        original, = originals
        snapshot, = snapshots
        if original["exit"] == 0 or snapshot["before_bytes"] <= 0 or snapshot["before_bytes"] != snapshot["after_bytes"] or snapshot["before_sha256"] != snapshot["after_sha256"]:
            issues.append("Original failure or checkpoint identity disagrees")
    if len(children) != 2 * len(failed_cases) or len(inputs) != len(failed_cases) or len(debug_logs) != len(failed_cases) or len(sites) != len(failed_cases):
        issues.append("Extra or missing conditional replay records")
    result = "INCONCLUSIVE_NOT_REPRODUCED"
    if CASE in failed_cases:
        result = "ORIGINAL_LEAF_PRINTER_FAILURE_REPRODUCED"
    elif failed_cases:
        result = "OTHER_PRINTER_FAILURE"
    elif focused_failed:
        result = "INCONCLUSIVE_FOCUSED_FAILURE_OUTSIDE_PRINTER"
    if issues:
        result = "INCOMPLETE_OBSERVATION"
    return dict(case_count=len(names), case_names_sha256=names_sha, census_matches_original=names == old_names,
                log_sha256=hashlib.sha256(log.read_bytes()).hexdigest(),
                xml_sha256=hashlib.sha256(xml.read_bytes()).hexdigest(), children=children, checkpoints=inputs,
                debug_logs=debug_logs, callsites=sites, focused_executed=focused_executed, focused_failed=focused_failed, errors=diagnostics[:8], omitted_error_lines=max(0, len(diagnostics) - 8),
                issues=issues, investigation_result=result, original_leaf_reproduced=CASE in failed_cases)


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
    receipt = dict(source=SOURCE, scope="Original full cmd owner; conditional same-checkpoint diagnostic only after failure",
                   production_source=PRODUCTION, source_run=41608714830, source_artifact=13218491847, status="INCOMPLETE", errors=[], started_at=now())
    report.write_json(OUT / "verdict.json", receipt)
    try:
        assert git("rev-parse", "HEAD").decode().strip() == SOURCE
        assert git("rev-parse", SOURCE + "^").decode().strip() == PRODUCTION
        assert git("diff", "--name-only", PRODUCTION, SOURCE).decode().strip() == "runsc/cmd/statefile_test.go"
        before = host("host.json")
        host_keys = ("machine", "kernel", "uid", "cgroup_v2", "apparmor_restrict_unprivileged_userns", "unprivileged_userns_clone")
        receipt["host_before"] = {key: before[key] for key in host_keys}
        receipt["original_failures"] = originals()
        probe = subprocess.run(["unshare", "-Ur", "true"], capture_output=True, text=True)
        receipt["namespace_probe"] = dict(exit=probe.returncode, stderr=probe.stderr[:1000])
        report.write_json(OUT / "verdict.json", receipt)
        assert before["apparmor_restrict_unprivileged_userns"] in (None, "0"), "Default worker restricts user namespaces"
        assert probe.returncode == 0, "Default worker cannot create the required user namespace"
        selected = SELECTED
        targets = OUT / "expected-targets.txt"
        targets.write_text("\n".join(sorted(selected)) + "\n")
        receipt.update(selected=len(selected), fixture_blob=git("rev-parse", SOURCE + ":runsc/cmd/statefile_test.go").decode().strip())
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
                       case_counts=v["case_counts"],
                       skips=[{**{key: row[key] for key in ("owner", "run", "shard", "attempt", "name", "status") if key in row},
                               "reason_sha256": hashlib.sha256(row.get("reason", "").encode()).hexdigest()} for row in v["skips"]],
                       zero=v["zero_case_owners"], all_skipped=v["all_skipped_owners"],
                       failed=[{**{key: row[key] for key in ("owner", "run", "shard", "attempt", "cases", "status") if key in row},
                                "category": {"XML test failures": "XML_TEST_FAILURE", "test result": "TEST_RESULT"}.get(row.get("reason"), "OTHER_VERIFIER_FAILURE"),
                                "reason_sha256": hashlib.sha256(row.get("reason", "").encode()).hexdigest()} for row in v["failures"]], native_exit=code)
        if invalid or selected != configured or not cpus or set(cpus.values()) != {"aarch64"} or attempted - selected:
            receipt["errors"].append("Source selection or native test placement disagrees; see detailed evidence")
        # Preserve the primary owner verdict before optional execution-log decoding.
        report.write_json(OUT / "verdict.json", receipt)
        after = host("host-after.json")
        receipt["host_after"] = {key: after[key] for key in host_keys}
        receipt["host_policy_unchanged"] = all(before[key] == after[key] for key in POLICY_KEYS)
        if not receipt["host_policy_unchanged"]:
            receipt["errors"].append("Host policy changed during the test invocation")
        receipt["child_diagnostics"] = child_diagnostics(directory)
        receipt["errors"].extend(receipt["child_diagnostics"]["issues"])
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
