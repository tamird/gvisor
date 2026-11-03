"""Complete the source-bound unit selection using the existing native v1 runners."""

import ast
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree
from zipfile import ZipFile

import qualification_report as report
import unit_matrix
from credential_artifacts import prepare
from stdlib_provenance import digest, fields, first, number

PRODUCT = "96fadde722e14d482a62276b80237398f8b62fbf"
GUEST = "38745c527784376e0b5d915fe5ebd85e1d1385fd"
SOURCE = os.environ["GITHUB_SHA"]
TMP = Path(os.environ["RUNNER_TEMP"])
PUBLIC = TMP / "unit-v1-public"
PACKAGE = ".github/qualification/unit-cgroup-v1"
WORKER = PACKAGE + "/worker"
CGROUP = "//runsc/cgroup:cgroup_test"
CORETAG = "//pkg/coretag:coretag_test"
FILTERS = ("-nogo", "-requires-kvm")
CONTROLLERS = set("cpuset cpu cpuacct blkio memory devices freezer net_cls perf_event net_prio hugetlb pids".split())
IMAGE = "docker://gcr.io/flame-public/rbe-ubuntu22-04@sha256:0d84a80bb0fc36ba5381942adcf6493249594dcc9044845c617b78c9b621cae3"
PROPS = {"Arch": "amd64", "OSFamily": "linux", "container-image": IMAGE,
         "workload-isolation-type": "firecracker", "recycle-runner": "false", "dockerUser": "root"}
HOOK = "399816e7c61acc27168b85a5a4d189e95bf86ea6e2eaaf46ffa17da447c916dd"
record = dict(status="INCOMPLETE", product=PRODUCT, carrier=SOURCE, guest_source=GUEST,
              phases={}, scope="355 exact unit owners; controlled native cgroup-v1 VMs, not a production Buildkite worker")
secrets = [v.encode() for k, v in os.environ.items() if re.search("TOKEN|SECRET|PASSWORD|CREDENTIAL|API_KEY", k) and len(v) > 16]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def native_disposition(kind: str, code: int) -> str:
    if code == 0:
        return "COMPLETED"
    if kind == "analysis" and code == 1:
        return "ANALYSIS_NO_BUILD_REQUIRES_BEP"
    if code in (8, 124, 130, 137, 143) or code < 0:
        return "INTERRUPTED_OR_TIMED_OUT"
    if code in (126, 127):
        return "BOOTSTRAP_FAILURE"
    return "FAIL"


def outcome(phases: dict) -> str:
    commands = [command for phase in phases.values() for command in phase.get("commands", {}).values()]
    if any(phase.get("suite_status") == "FAIL" for phase in phases.values()) or any(
            native_disposition(command["kind"], command["native_exit"]) == "FAIL" for command in commands):
        return "FAIL"
    if not phases or any(phase["status"] != "PASS" or phase.get("suite_skips") for phase in phases.values()):
        return "INCOMPLETE"
    return "PASS"


def classification_controls(row: dict) -> None:
    controls = [
        ("test_failure_before_capture", "test", 3, "INCOMPLETE", "FAIL"),
        ("build_failure_before_capture", "build", 1, "INCOMPLETE", "FAIL"),
        ("preflight_evidence_missing", "test", 0, "INCOMPLETE", "INCOMPLETE"),
        ("preflight_timeout", "test", 124, "INCOMPLETE", "INCOMPLETE"),
        ("bootstrap_missing_bazel", "test", 127, "INCOMPLETE", "INCOMPLETE"),
        ("verified_no_build_selection", "analysis", 1, "PASS", "PASS"),
        ("unverified_no_build_selection", "analysis", 1, "INCOMPLETE", "INCOMPLETE"),
        ("completed_execution", "test", 0, "PASS", "PASS"),
    ]
    row["synthetic_only"] = True
    row["cases"] = []
    for name, kind, code, status, expected in controls:
        actual = outcome({"control": dict(status=status, commands={"original": dict(kind=kind, native_exit=code)})})
        row["cases"].append(dict(name=name, actual=actual, expected=expected))
        assert actual == expected, "native failure classification changed"


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL)


def save() -> None:
    record["verdict_available_at"] = now()
    data = json.dumps(record, sort_keys=True).encode() + b"\n"
    assert len(data) < 8 * 1024**2, "bounded public receipt exceeded"
    assert not any(secret in data for secret in secrets), "credential in receipt"
    assert not re.search(rb'(?i)\b(?:authorization|x-buildbuddy-api-key)["\x27]?\s*[:=]|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|BEGIN (RSA |OPENSSH )?PRIVATE KEY', data)
    PUBLIC.mkdir(exist_ok=True)
    (PUBLIC / "verdict.tmp").write_bytes(data)
    (PUBLIC / "verdict.tmp").replace(PUBLIC / "verdict.json")


def phase(name: str, function) -> bool:
    row = record["phases"][name] = dict(status="INCOMPLETE", started_at=now())
    save()
    try:
        function(row)
        row["status"] = "PASS"
    except Exception as error:
        row.update(status="FAIL" if outcome({name: row}) == "FAIL" else "INCOMPLETE",
                   error_class=type(error).__name__, error_sha256=sha(str(error).encode()))
        locations = [dict(function=frame.name, line=frame.lineno) for frame in traceback.extract_tb(error.__traceback__)
                     if frame.filename == __file__]
        row["error_locations"] = locations[-2:]
    row["finished_at"] = now()
    save()
    print(name, row["status"], flush=True)
    return row["status"] == "PASS"


def command(row: dict, name: str, args: list[str], minutes: int) -> int:
    started_at = now()
    with (TMP / (name + ".console")).open("wb") as stream:
        proc = subprocess.run(["timeout", "--signal=INT", "--kill-after=30s", f"{minutes}m", *args],
                              stdout=stream, stderr=subprocess.STDOUT)
    row.update(native_exit=proc.returncode, execution_finished_at=now())
    console = (TMP / (name + ".console")).read_bytes()
    markers = {"fetch": b"Error downloading", "permission": b"Permission denied", "disk": b"No space left on device",
               "memory": b"out of memory", "timeout": b"timed out", "test_failed": b"FAILED",
               "analysis": b"Analysis of target", "missing_package": b"no such package"}
    row.update(console_sha256=sha(console), console_bytes=len(console),
               diagnostic_classes=[k for k, v in markers.items() if v in console])
    row.setdefault("commands", {})[name] = dict(started_at=started_at, **{
        key: row[key] for key in ("native_exit", "execution_finished_at", "console_sha256", "console_bytes", "diagnostic_classes")})
    row["commands"][name]["kind"] = "analysis" if "--nobuild" in args else next(arg for arg in args if arg in ("test", "build"))
    row["commands"][name]["native_disposition"] = native_disposition(row["commands"][name]["kind"], proc.returncode)
    save()
    return proc.returncode


def events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_bytes().splitlines() if line]


def unit_tags(tags: list[str]) -> bool:
    return not set(tags).intersection(tag.removeprefix("-") for tag in FILTERS)


def originals(row: dict) -> None:
    data = (TMP / "old-cgroup.zip").read_bytes()
    assert sha(data) == "e3f63ab35bfc05652e0a93b2d1715b4cef319f6a1ac47f0911bde4a8196578fc"
    with ZipFile(BytesIO(data)) as archive:
        assert archive.namelist() == ["unit-cgroup-v1.json"]
        old = json.loads(archive.read("unit-cgroup-v1.json"))
    assert old["product"] == PRODUCT and old["owner"] == CGROUP and old["guest_source"] == GUEST
    assert old["native_exits"]["guest"] == 0 and old["native_exits"]["product-cache-selection"] == 1
    assert old["status"] == "FAIL" and old["xml"]["passed"] == 156 and old["xml"]["failed"] == old["xml"]["skipped"] == 0
    assert set(old["guest"]["mounts"]) == CONTROLLERS | {"name=systemd"}
    assert old["guest_attempts"] == [dict(status="PASSED", cached_local=False, cached_remote=False, strategy="local")]
    row.update(cgroup_original=old, cgroup_zip_sha256=sha(data), reused_owner=CGROUP)
    data = (TMP / "old-selection.zip").read_bytes()
    assert sha(data) == "b247ccca19bdb5f7b8a6f26d065661ed2d6de782aa080b5bcc64bc86fcd62757"
    with ZipFile(BytesIO(data)) as archive:
        assert archive.read("native/final_head").decode().strip() == PRODUCT
        assert archive.read("native/commit_file") == git("cat-file", "commit", PRODUCT)
        selected = json.loads(archive.read("native/selected.json"))
    names = sorted(r[0] for r in selected["configured"] if r[0] in selected["tests"] and not r[1]
                   and unit_tags(r[3]))
    assert len(names) == len(set(names)) == 355 and {CGROUP, CORETAG} <= set(names)
    (TMP / "expected.txt").write_text("\n".join(names) + "\n")
    row.update(selection_zip_sha256=sha(data), expected=names, expected_sha256=sha((TMP / "expected.txt").read_bytes()))


def configured(path: Path, code: int) -> dict:
    all_tests = unit_matrix.configured_targets(str(path), tests=True, allow_test_no_build=True, expected_exit=code)
    return {label: target for label, target in all_tests.items() if unit_tags(target.tags)}


def python_cases(directory: Path, reports: list[dict]) -> dict:
    source = Path("sandboxexec/sandbox/python/tests/sandbox_test.py").read_bytes()
    methods = sorted(node.name for node in ast.walk(ast.parse(source))
                     if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"))
    assert len(methods) == len(set(methods)) == 39
    item, = [item for item in reports if item["identity"]["label"] == "//sandboxexec/sandbox/python:sandbox_py_test"
             and item["name"] == "test.log"]
    data = (directory / item["path"]).read_bytes()
    assert sha(data) == item["sha256"] and len(data) == item["bytes"]
    text = data.decode()
    counts = re.findall(r"^Ran ([0-9]+) tests? in ", text, re.M)
    statuses = re.findall(r"^(OK(?: \(.*\))?|FAILED \(.*\))$", text, re.M)
    return dict(source_sha256=sha(source), expected_methods=methods, log_sha256=sha(data),
                observed_counts=counts, terminal_statuses=statuses,
                complete=counts == [str(len(methods))] and statuses == ["OK"])


def modes(path: Path, prefix: str = "") -> dict:
    with ZipFile(path) as archive:
        def read(name: str) -> list[str]:
            info = archive.getinfo(prefix + name)
            assert info.file_size < 65536
            return archive.read(info).decode().splitlines()
        memberships = {}
        for line in read("v1-membership.txt"):
            if ":" in line:
                hid, controller, location = line.split(":", 2)
                if controller in CONTROLLERS | {"name=systemd"}:
                    assert location == "/" and int(hid) > 0
                    memberships[controller] = int(hid)
        mounts = {}
        for line in read("v1-mounts.txt"):
            before, after = line.split(" - ")
            kind, _device, options = after.split()
            if kind != "cgroup":
                continue
            fields = before.split()
            name = fields[4].removeprefix("/sys/fs/cgroup/")
            controller = "name=systemd" if name == "systemd" else name
            assert fields[3] == "/" and controller in options.split(",")
            mounts[controller] = memberships[controller]
        assert set(memberships) == set(mounts) == CONTROLLERS | {"name=systemd"}
        assert len(set(mounts.values())) == 13
        result = dict(output_zip_sha256=sha(path.read_bytes()), mounts=mounts)
        result["kernel"], = read("kernel-release.txt")
        assert re.fullmatch(r"[A-Za-z0-9._+-]+", result["kernel"])
        if prefix + "owner-mode" in archive.namelist():
            mode, uid, kernel, machine = read("owner-mode")
            assert mode == "false" and int(uid) > 0 and machine == "x86_64"
            result.update(mode=mode, uid=int(uid), kernel=kernel, machine=machine)
        if prefix + "owner-elf.sha256" in archive.namelist():
            result["elf_sha256"], = read("owner-elf.sha256")
        return result


def placements(path: Path, expected: set[str], wrapper_sha: str, invocation: str) -> list[dict]:
    files, inputs, rows = {}, {}, []
    invocations = []
    def integers(values):
        result = []
        for value in values:
            if isinstance(value, int):
                result.append(value)
            else:
                stream = BytesIO(value)
                while (item := number(stream)) is not None:
                    result.append(item)
        return result
    with subprocess.Popen(["zstd", "-qdc", str(path)], stdout=subprocess.PIPE) as process:
        while (size := number(process.stdout)) is not None:
            entry = fields(process.stdout.read(size))
            key = first(entry, 1, 0)
            if 2 in entry:
                invocations.append(first(fields(first(entry, 2)), 4).decode())
            if 3 in entry:
                item = fields(first(entry, 3))
                files[key] = (first(item, 1).decode(), digest(first(item, 2)))
            if 6 in entry:
                item = fields(first(entry, 6))
                inputs[key] = (integers(item.get(5, [])), integers(item.get(4, [])))
            if 9 in entry:
                item = fields(first(entry, 9))
                inputs[key] = ([first(fields(v), 2, 0) for v in item.get(1, [])], integers(item.get(2, [])))
            if 10 in entry:
                item = fields(first(entry, 10))
                inputs[key] = ([], [first(item, n, 0) for n in (2, 3, 4)])
            if 7 not in entry:
                continue
            spawn = fields(first(entry, 7))
            owner = first(spawn, 7).decode().removeprefix("@@").removeprefix("@")
            args = [a.decode() for a in spawn.get(1, [])]
            if first(spawn, 8) != b"TestRunner" or not args or not args[0].endswith("/tools/test/test-setup.sh"):
                continue
            assert owner in expected, "unexpected actual TestRunner"
            props = {first(fields(v), 1).decode(): first(fields(v), 2).decode() for v in fields(first(spawn, 3)).get(1, [])}
            pending, visited, ids = [first(spawn, 4, 0)], set(), set()
            while pending:
                item = pending.pop()
                if not item or item in visited:
                    continue
                visited.add(item)
                if item in files:
                    ids.add(item)
                elif item in inputs:
                    direct, transitive = inputs[item]
                    pending.extend(direct + transitive)
            declared = {files[i][0]: files[i][1] for i in ids if i in files}
            bound = [dict(path=p, digest=d) for p, d in declared.items() if p.endswith("/cgroup_v1.sh") and d[0] == wrapper_sha]
            row = dict(owner=owner, runner=first(spawn, 11).decode(), cached=bool(first(spawn, 12, 0)),
                       exit=first(spawn, 9, 0), status=first(spawn, 10).decode(), digest=digest(first(spawn, 16)),
                       platform={k: props.get(k) for k in PROPS}, wrapper_inputs=bound, argv_sha256=sha(json.dumps(args).encode()))
            row["verified"] = (row["runner"] == "remote" and not row["cached"] and row["platform"] == PROPS
                               and bool(bound) and any(WORKER + "/wrapper" in a or WORKER + "/preflight" in a for a in args)
                               and re.fullmatch("[0-9a-f]{64}", row["digest"][0]) is not None and row["digest"][1] > 0)
            rows.append(row)
        assert process.wait() == 0, "incomplete compact log"
    assert invocations == [invocation], "compact log belongs to a different invocation"
    return rows


def execute(row: dict, name: str, options: list[str], expected: set[str], minutes: int, *, remote: bool) -> None:
    directory = TMP / name
    directory.mkdir()
    raw, compact = TMP / (name + ".events"), TMP / (name + ".compact")
    source = git("rev-parse", "HEAD").decode().strip()
    command_args = ["test", *options, "--nocache_test_results", "--flaky_test_attempts=1", "--runs_per_test=1",
                    "--test_env=GO_TEST_WRAP_TESTV=true", "--zip_undeclared_test_outputs",
                    f"--build_event_json_file={raw}", f"--execution_log_compact_file={compact}"]
    (directory / "executed_arguments").write_text("\n".join(command_args) + "\n")
    (directory / "commit_file").write_bytes(git("cat-file", "commit", source))
    (directory / "source_before").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    code = command(row, name, ["bazel", f"--bazelrc={TMP}/v1.bazelrc", *command_args], minutes)
    (directory / "native_exit").write_text(str(code))
    (directory / "source_after").write_bytes(git("status", "--porcelain", "--untracked-files=no"))
    (directory / "final_head").write_bytes(git("rev-parse", "HEAD"))
    captured = report.capture(raw, directory / "events.jsonl", Path("bazel-out"), directory / "reports", retain_outputs=True)
    (directory / "capture_exit").write_text("1" if captured["errors"] else "0")
    context = {key: key for key in ("executed_arguments", "commit_file", "source_before", "source_after", "native_exit", "capture_exit", "final_head")}
    context.update(expected_source=source, command_kind="test", execution_finished_at=row["execution_finished_at"])
    report.write_json(directory / "context.json", context)
    targets = directory / "expected.txt"
    targets.write_text("\n".join(sorted(expected)) + "\n")
    verdict = report.verdict(directory / "events.jsonl", report.manifest_path(directory / "events.jsonl"), directory / "context.json", targets)
    report.write_json(directory / "verdict.json", verdict)
    for key in ("status", "case_counts", "zero_case_owners", "all_skipped_owners", "missing", "skips", "failures", "executed"):
        row["suite_" + key] = verdict[key]
    row["suite_errors"] = [sha(error.encode()) for error in verdict["errors"]]
    row["original_reports"] = [{key: item[key] for key in ("identity", "name", "sha256", "bytes")}
                               for item in captured["reports"]
                               if item["name"] in {"test.log", "test.xml", "test.outputs__outputs.zip", "test.outputs/outputs.zip"}]
    row["events_sha256"] = sha(raw.read_bytes())
    row["compact_sha256"] = sha(compact.read_bytes())
    original_events = events(raw)
    row["invocation"], = [e["started"]["uuid"] for e in original_events if "started" in e]
    save()
    if "//sandboxexec/sandbox/python:sandbox_py_test" in expected:
        try:
            row["python_unittest"] = python_cases(directory, captured["reports"])
        except Exception as error:
            row["python_unittest"] = dict(complete=False, error_class=type(error).__name__, error_sha256=sha(str(error).encode()))
        save()
    if remote:
        rows = placements(compact, expected, record["wrapper_sha256"], row["invocation"])
        row["placements"] = rows
        assert Counter(r["owner"] for r in rows) == Counter(r["owner"]["label"] for r in verdict["executed"]), "compact and BEP attempt counts differ"
        assert {r["owner"] for r in rows} == expected and all(r["verified"] for r in rows), "missing or wrong worker placement"
        mounted = row["mounted"] = []
        mount_errors = row["mount_errors"] = []
        for event in original_events:
            if "testResult" not in event:
                continue
            identity = event["id"]["testResult"]
            owner = identity["label"]
            if owner not in expected:
                continue
            try:
                candidates = [f for f in event["testResult"].get("testActionOutput", []) if f.get("name") in {"test.outputs__outputs.zip", "test.outputs/outputs.zip"}]
                assert len(candidates) == 1, "missing v1 controller receipt"
                uri = urlsplit(candidates[0]["uri"])
                assert uri.scheme == "file", "controller receipt not materialized"
                path = Path(unquote(uri.path))
                assert path.resolve().is_relative_to(Path("bazel-out").resolve())
                mounted.append(dict(owner=owner, identity=identity, **modes(path)))
            except Exception as error:
                mount_errors.append(dict(identity=identity, error_class=type(error).__name__, error_sha256=sha(str(error).encode())))
        save()
        assert not mount_errors, "invalid mount evidence"
        assert len(mounted) == len(verdict["executed"]), "missing mount evidence for an attempt"
        if name == "preflight":
            assert len(mounted) == 1 and mounted[0].get("mode") == "false", "preflight payload did not observe native v1"
    else:
        assert len(verdict["executed"]) == 1, "missing unique native guest attempt"
        attempt, = verdict["executed"]
        assert not attempt["cached_locally"] and not attempt["execution_info"].get("cachedRemotely")
        assert attempt["execution_info"].get("strategy") == "local" and attempt["run"] == attempt["attempt"] == 1
    save()
    assert code == 0 and verdict["status"] == "PASS", "native owner failed or incomplete"
    assert row.get("python_unittest", {}).get("complete", True), "Python unittest did not execute all 39 methods successfully"
    assert not verdict["all_skipped_owners"], "all-skipped owner is not coverage"
    assert all(owner["label"] == "//:build_test" for owner in verdict["zero_case_owners"]), "unexpected zero-case owner"


def run() -> None:
    assert git("rev-parse", "HEAD").decode().strip() == SOURCE and git("rev-parse", "HEAD^").decode().strip() == PRODUCT
    assert set(git("diff", "--name-only", PRODUCT, SOURCE).decode().splitlines()) == {PACKAGE + "/" + n for n in ("check.py", "worker.BUILD", "guest.BUILD", "payload.sh")} | {".github/workflows/native-unit-amd64-cgroup-v1.yml"}
    assert os.uname().machine == "x86_64" and os.getuid() != 0
    assert subprocess.run(["sudo", "-n", "sh", "-c", "test -c /dev/kvm && test -r /dev/kvm && test -w /dev/kvm"]).returncode == 0
    record["coordinator"] = dict(machine=os.uname().machine, kernel=os.uname().release, uid=os.getuid(),
                                 kvm_access=True, cgroup_v2=Path("/sys/fs/cgroup/cgroup.controllers").is_file())
    saved = {n: Path(PACKAGE, n).read_bytes() for n in ("worker.BUILD", "guest.BUILD", "payload.sh")}
    git("fetch", "--quiet", "--depth=1", "origin", GUEST)
    wrapper = git("show", GUEST + ":test/rbe/cgroup_v1.sh")
    record["wrapper_sha256"] = sha(wrapper)
    assert phase("verdict_controls", classification_controls)
    assert phase("reused_originals", originals)
    expected = set((TMP / "expected.txt").read_text().splitlines())
    git("switch", "--quiet", "--detach", PRODUCT)
    assert [line.split("--test_tag_filters=", 1)[1].split()[0] for line in Path(".bazelrc").read_text().splitlines()
            if line.startswith("test:unit ") and "--test_tag_filters=" in line] == [",".join(FILTERS)]
    hook = Path(".buildkite/hooks/pre-command").read_bytes()
    start = b"  if [[ -f /sys/fs/cgroup/cgroup.controllers ]]; then\n"
    assert hook.count(start) == 1
    block = hook[hook.index(start):].split(b"  fi\n", 1)[0] + b"  fi\n"
    assert sha(block) == HOOK
    Path(WORKER).mkdir(parents=True)
    for name, data in {"BUILD": saved["worker.BUILD"], "payload.sh": saved["payload.sh"], "hook.bash": block, "cgroup_v1.sh": wrapper}.items():
        Path(WORKER, name).write_bytes(data)
    Path(WORKER, "payload.sh").chmod(0o755)
    Path(WORKER, "cgroup_v1.sh").chmod(0o755)
    common = ["--extra_execution_platforms=//" + WORKER + ":worker", "--jobs=16", "--spawn_strategy=remote",
              "--strategy=TestRunner=remote", "--noremote_local_fallback", "--remote_download_outputs=toplevel",
              "--remote_download_regex=.*test.outputs/outputs.zip", "--test_env=GVISOR_CGROUP_TEST_USER=nobody"]
    os.environ["CGROUPV2"] = "false"
    def analysis(row):
        raw = TMP / "analysis.events"
        code = command(row, "analysis", ["bazel", f"--bazelrc={TMP}/v1.bazelrc", "test", "--config=buildkite-unit", *common,
                                          "--nobuild", f"--build_event_json_file={raw}"], 20)
        selected = configured(raw, code)
        row.update(selected={k: asdict(v) for k, v in sorted(selected.items())}, events_sha256=sha(raw.read_bytes()),
                   analysis_only=True, no_build_exit_accepted=(code == 1))
        assert set(selected) == expected, "original 355-owner selection changed"
    selection_ok = phase("analysis", analysis)
    preflight = "//" + WORKER + ":preflight"
    preflight_ok = phase("firecracker_preflight", lambda row: execute(row, "preflight", ["--config=buildkite", *common, preflight], {preflight}, 15, remote=True))
    if selection_ok and preflight_ok:
        roots = Path("tools/unit_test_targets.txt").read_text()
        targets = TMP / "remaining.targets"
        targets.write_text(roots + "\n-" + CGROUP + "\n-" + CORETAG + "\n")
        phase("remaining353", lambda row: execute(row, "remaining", ["--config=buildkite-unit", *common,
              "--run_under=//" + WORKER + ":wrapper", f"--target_pattern_file={targets}"], expected - {CGROUP, CORETAG}, 100, remote=True))
    else:
        record["phases"]["remaining353"] = dict(status="NOT_RUN", reason="selection or native v1 worker preflight failed", missing=sorted(expected - {CGROUP, CORETAG}))
        save()
    def coretag(row):
        assert command(row, "coretag-build", ["bazel", f"--bazelrc={TMP}/v1.bazelrc", "build", "--config=ci", "--strip=never", *common, CORETAG], 25) == 0
        files = subprocess.check_output(["bazel", f"--bazelrc={TMP}/v1.bazelrc", "cquery", "--config=ci", "--strip=never", *common, "--output=files", CORETAG], stderr=subprocess.DEVNULL, timeout=600).decode().splitlines()
        path, = files
        data = Path(path).read_bytes()
        assert data[:6] == b"\x7fELF\x02\x01" and int.from_bytes(data[18:20], "little") == 62
        row["original_elf"] = dict(sha256=sha(data), size=len(data), owner=CORETAG)
        shutil.rmtree(WORKER)
        git("switch", "--quiet", "--detach", GUEST)
        Path(WORKER).mkdir(parents=True)
        for name, body in {"BUILD": saved["guest.BUILD"], "payload.sh": saved["payload.sh"], "hook.bash": block, "selected-test": data}.items():
            Path(WORKER, name).write_bytes(body)
        Path(WORKER, "payload.sh").chmod(0o755)
        Path(WORKER, "selected-test").chmod(0o755)
        owner = "//" + WORKER + ":guest_test"
        execute(row, "coretag-guest", ["--config=rbe", "--config=rbe-local-tests", "--//tools/bazeldefs:local_test_architecture=amd64",
                "--jobs=16", "--test_timeout=420", owner], {owner}, 25, remote=False)
        directory = Path("bazel-testlogs", WORKER, "guest_test")
        cases = list(ElementTree.parse(directory / "test.xml").iter("testcase"))
        assert len(cases) == 1 and cases[0].attrib["name"] == "TestEnable" and not list(cases[0]), "coretag did not execute successfully"
        log = (directory / "test.log").read_text()
        assert "phase=guest_status_read status=0" in log and "CONFIG_SCHED_CORE=y" in log
        assert all(f"Guest CPU {cpu} thread siblings: 0-1" in log for cpu in (0, 1))
        row["guest"] = modes(directory / "test.outputs/outputs.zip", "guest/outputs/")
        assert row["guest"]["elf_sha256"] == row["original_elf"]["sha256"]
        row["native_kvm_smt"] = True
    if selection_ok and preflight_ok:
        phase("coretag_native_kvm", coretag)
    else:
        record["phases"]["coretag_native_kvm"] = dict(status="NOT_RUN", missing=[CORETAG])
    record["status"] = outcome(record["phases"])
    record["skipped_cases_remain_unqualified"] = sum(len(p.get("suite_skips", [])) for p in record["phases"].values())
    record["functional_failure"] = any(p.get("suite_status") == "FAIL" or any(
        command["kind"] == "test" and command["native_exit"] == 3 for command in p.get("commands", {}).values())
        for p in record["phases"].values())
    save()


if __name__ == "__main__":
    save()
    try:
        with tempfile.TemporaryDirectory(dir=TMP) as directory:
            prepare(Path(directory))
            del os.environ["BUILDBUDDY_API_KEY"]
            rc = TMP / "v1.bazelrc"
            rc.write_text(f"common --credential_helper=remote.buildbuddy.io={directory}/helper\n"
                          "build --remote_executor=grpcs://remote.buildbuddy.io\nbuild --remote_cache=grpcs://remote.buildbuddy.io\n"
                          "build --bes_backend=grpcs://remote.buildbuddy.io\nbuild --bes_results_url=https://app.buildbuddy.io/invocation/\n"
                          "build:buildbuddy_remote_executor --remote_executor=grpcs://remote.buildbuddy.io\n")
            run()
    except Exception as error:
        record.update(status="FAIL" if outcome(record["phases"]) == "FAIL" else "INCOMPLETE",
                      error_class=type(error).__name__, error_sha256=sha(str(error).encode()))
        save()
    sys.exit(0 if record["status"] == "PASS" else 1)
