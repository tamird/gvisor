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
from urllib.request import urlopen
from xml.etree import ElementTree
from zipfile import ZipFile

import qualification_report as report
import unit_matrix
from credential_artifacts import prepare
from stdlib_provenance import digest, fields, first, number

PRODUCT = "96fadde722e14d482a62276b80237398f8b62fbf"
ORIGINAL = "079301e2078f436c47e55cc6ef4815d0bcbc25c3"
PARENT = "6c53cad9aa6f059858196cda65643cd623580700"
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


def diagnostics(data: bytes) -> list[dict]:
    """Project repository failures without publishing raw lines or URL secrets."""
    result = []
    patterns = {"download": r"Error downloading", "timeout": r"timed out|TimeoutException",
                "dns": r"UnknownHostException", "tls": r"SSLHandshakeException|PKIX",
                "checksum": r"[Cc]hecksum.*(?:match|expected)", "package": r"no such package",
                "http": r"(?i)(?:HTTP(?:/[0-9.]+)?\s+|(?:status(?:\s+code)?|response code)\s*[:=]?\s*)[45][0-9]{2}\b"}
    for line in data.decode(errors="replace").splitlines():
        if re.search(r"(?i)\b(?:authorization|x-buildbuddy-api-key)[\"']?\s*[:=]", line):
            continue
        classes = [name for name, pattern in patterns.items() if re.search(pattern, line)]
        if not classes:
            continue
        urls = []
        for value in re.findall(r"https?://[^\s\]\"']+", line):
            try:
                url = urlsplit(value)
            except ValueError:
                continue
            basename = Path(url.path).name
            if url.hostname and re.fullmatch(r"[A-Za-z0-9.-]{1,200}", url.hostname):
                archive_name = re.fullmatch(r"[A-Za-z0-9._+-]{1,110}\.(?:tar(?:\.(?:gz|xz|bz2|zst))?|tgz|zip|whl|gz|xz|json|mod|sum|bazel)", basename)
                urls.append(dict(host=url.hostname, filename=basename if archive_name else None))
        result.append(dict(classes=classes, line_sha256=sha(line.encode()), urls=urls,
                           repositories=re.findall(r"no such package ['\"](@@[A-Za-z0-9_+~.-]+)//", line),
                           http_codes=re.findall(r"(?i)(?:HTTP(?:/[0-9.]+)?\s+|(?:status(?:\s+code)?|response code)\s*[:=]?\s*)([45][0-9]{2})\b", line)))
        if len(result) == 8:
            break
    return result


def musl_archive(row: dict) -> None:
    """Seed only the failed archive; Bazel still verifies its supplier SRI."""
    data = (TMP / "prior-analysis.zip").read_bytes()
    assert sha(data) == "2cb45232ed46c1d2ac45e9451afc95444182ba56caf66f289024f47ef2d6f833"
    with ZipFile(BytesIO(data)) as archive:
        assert archive.namelist() == ["verdict.json"]
        old = json.loads(archive.read("verdict.json"))
    assert old["carrier"] == PARENT and old["product"] == PRODUCT and old["status"] == "INCOMPLETE"
    assert old["phases"]["analysis"]["native_exit"] == 1
    assert old["phases"]["remaining353"]["status"] == old["phases"]["coretag_native_kvm"]["status"] == "NOT_RUN"
    row.update(prior_zip_sha256=sha(data), prior_analysis=old["phases"]["analysis"],
               prior_lookup=old["original_analysis_lookup"], prior_verdict=old["status"])
    save()
    url = "https://deb.debian.org/debian/pool/main/m/musl/musl_1.2.6.orig.tar.gz"
    with urlopen(url, timeout=60) as response:
        data = response.read(2 * 1024**2 + 1)
    expected = "d585fd3b613c66151fc3249e8ed44f77020cb5e6c1e635a616d3f9f82460512a"
    assert sha(data) == expected and len(data) == 1082499, "mirror differs from llvm v0.8.24 musl archive"
    destination = TMP / "v1-distdir"
    destination.mkdir()
    (destination / "musl-1.2.6.tar.gz").write_bytes(data)
    with (TMP / "v1.bazelrc").open("a") as rc:
        rc.write(f"common --distdir={destination}\n")
    row.update(mirror=url, original_url="https://musl.libc.org/releases/musl-1.2.6.tar.gz",
               supplier="llvm@0.8.24", supplier_integrity="sha256-1YX9O2E8ZhUfwySejtRPdwIMtebB5jWmFtP5+CRgUSo=",
               sha256=expected, bytes=len(data), mechanism="Bazel --distdir; unchanged repository declaration")


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
    observed = diagnostics(b"Error downloading https://user:private@github.com/org/repo/archive.tar.gz?token=private: Read timed out\nAuthorization: Bearer private\nno such package '@@module+//path?private'\n")
    assert len(observed) == 2 and observed[0]["classes"] == ["download", "timeout"]
    assert observed[0]["urls"] == [dict(host="github.com", filename="archive.tar.gz")]
    assert observed[1]["repositories"] == ["@@module+"]
    assert "private" not in json.dumps(observed) and "user" not in json.dumps(observed)
    assert diagnostics(b"external/bazel_tools/tools/build_defs/repo/http.bzl:444:45") == []
    row["diagnostic_projection_control"] = "PASS"


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
    kind = "analysis" if "--nobuild" in args else next(arg for arg in args if arg in ("test", "build"))
    native = row.setdefault("commands", {})[name] = dict(started_at=started_at, kind=kind,
        native_exit=proc.returncode, execution_finished_at=row["execution_finished_at"],
        native_disposition=native_disposition(kind, proc.returncode))
    save()
    console = (TMP / (name + ".console")).read_bytes()
    markers = {"fetch": b"Error downloading", "permission": b"Permission denied", "disk": b"No space left on device",
               "memory": b"out of memory", "timeout": b"timed out", "test_failed": b"FAILED",
               "analysis": b"Analysis of target", "missing_package": b"no such package"}
    row.update(console_sha256=sha(console), console_bytes=len(console),
               diagnostic_classes=[k for k, v in markers.items() if v in console], diagnostics=diagnostics(console))
    native.update({key: row[key] for key in ("console_sha256", "console_bytes", "diagnostic_classes")})
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


def reuse_preflight(row: dict) -> None:
    data = (TMP / "old-preflight.zip").read_bytes()
    assert sha(data) == "dfccf150c539a0c8a235f22947e22bb453abbd5b5011a488d761863090507143"
    with ZipFile(BytesIO(data)) as archive:
        assert archive.namelist() == ["verdict.json"]
        old = json.loads(archive.read("verdict.json"))
    assert old["carrier"] == ORIGINAL and old["product"] == PRODUCT and old["status"] == "INCOMPLETE"
    assert old["wrapper_sha256"] == record["wrapper_sha256"]
    proof = old["phases"]["firecracker_preflight"]
    assert proof["status"] == proof["suite_status"] == "PASS" and proof["native_exit"] == 0
    assert proof["suite_case_counts"] == dict(passed=1, failed=0, skipped=0)
    placement, = proof["placements"]
    assert placement["verified"] and placement["platform"] == PROPS and not placement["cached"]
    assert placement["runner"] == "remote" and placement["exit"] == 0 and not placement["status"]
    mounted, = proof["mounted"]
    assert mounted["mode"] == "false" and mounted["machine"] == "x86_64" and mounted["uid"] > 0
    assert set(mounted["mounts"]) == CONTROLLERS | {"name=systemd"} and len(set(mounted["mounts"].values())) == 13
    row.update(original_zip_sha256=sha(data), original_verdict=old["status"], original_analysis=old["phases"]["analysis"],
               original_preflight=proof, workload_repeated=False)


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
    assert git("rev-parse", "HEAD").decode().strip() == SOURCE and git("rev-parse", "HEAD^").decode().strip() == PARENT
    assert set(git("diff", "--name-only", ORIGINAL, SOURCE).decode().splitlines()) == {PACKAGE + "/check.py", ".github/workflows/native-unit-amd64-cgroup-v1.yml"}
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
        raw_events = events(raw)
        row.update(events_sha256=sha(raw.read_bytes()),
                   invocations=[event["started"]["uuid"] for event in raw_events if "started" in event])
        save()
        selected = configured(raw, code)
        row.update(selected={k: asdict(v) for k, v in sorted(selected.items())}, events_sha256=sha(raw.read_bytes()),
                   analysis_only=True, no_build_exit_accepted=(code == 1))
        assert set(selected) == expected, "original 355-owner selection changed"
    archive_ok = phase("musl_archive", musl_archive)
    selection_ok = archive_ok and phase("analysis", analysis)
    if not archive_ok:
        record["phases"]["analysis"] = dict(status="NOT_RUN", reason="pinned archive unavailable")
    preflight_ok = phase("reused_firecracker_preflight", reuse_preflight)
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
    if preflight_ok:
        phase("coretag_native_kvm", coretag)
    else:
        record["phases"]["coretag_native_kvm"] = dict(status="NOT_RUN", missing=[CORETAG])
    record["status"] = outcome(record["phases"])
    record["skipped_cases_remain_unqualified"] = sum(len(p.get("suite_skips", [])) for p in record["phases"].values())
    record["functional_failure"] = any(p.get("suite_status") == "FAIL" or any(
        command["kind"] == "test" and command["native_exit"] == 3 for command in p.get("commands", {}).values())
        for p in record["phases"].values())
    save()


def recover_original() -> None:
    """Inspect the failed original invocations without executing another test."""
    from cas_inputs import blob, fetch, rpc

    original = "aaed41e45c71fb16ddb3ec4b6f630e15a60df757"
    assert git("rev-parse", "HEAD^^").decode().strip() == original
    assert set(git("diff", "--name-only", original, SOURCE).decode().splitlines()) == {
        PACKAGE + "/check.py", ".github/workflows/native-unit-amd64-cgroup-v1.yml"}
    raw = (TMP / "original.zip").read_bytes()
    assert sha(raw) == "2da258561d341293458e0021f0bb8f2124033b3e87d512848c018198c1655dd0"
    with ZipFile(BytesIO(raw)) as archive:
        assert archive.namelist() == ["verdict.json"]
        old = json.loads(archive.read("verdict.json"))
    assert old["carrier"] == original and old["product"] == PRODUCT and old["status"] == "FAIL"
    bulk, guest = [old["phases"][name] for name in ("remaining353", "coretag_native_kvm")]
    assert bulk["native_exit"] == 1 and guest["native_exit"] == 3
    assert len({a["owner"]["label"] for a in bulk["suite_executed"]}) == 353
    record.update(original_zip_sha256=sha(raw), original=old, workload_repeated=False,
                  scope="Original v1 diagnostics and timing only; original FAIL unchanged")
    save()
    catalog = git("show", PRODUCT + ":tools/licensecheck/dependencies.yaml").decode()
    dependencies = set(re.findall(r"(?m)^\s*- dependency: ([A-Za-z0-9_./+-]+)$", catalog))
    assert dependencies

    def project(data):
        # Output fixed diagnostic classes and source-known dependency names,
        # never arbitrary messages, commands, environment or checkpoint text.
        patterns = {
            "permission_denied": r"(?i)permission denied|operation not permitted|EACCES",
            "missing_file": r"(?i)no such file or directory|file not found|command not found|not found in .?PATH",
            "exec_format": r"(?i)exec format error|cannot execute binary file",
            "kvm_unavailable": r"(?i)could not access KVM|failed to initialize kvm|KVM.*not supported",
            "cpu_topology": r"(?i)CPU topology|thread siblings|requires.*SMT|doesn't support requested feature",
            "missing_shared_library": r"(?i)error while loading shared libraries|GLIBC_[0-9.]+.*not found",
            "sudo_password": r"(?i)sudo:.*password|sudo:.*terminal",
            "nogo_diagnostic": r"(?i)nogo:|GoStaticAnalysis|static analysis failed",
            "source_analysis": r"(?i)error running subcommand|failed to (?:compile|link)|compilation terminated",
            "kernel_panic": r"Kernel panic|not syncing|VFS: Unable to mount root",
            "timeout": r"(?i)timed out|TIMEOUT|deadline exceeded",
            "qemu_error": r"(?i)qemu-system.*(?:error|failed|could not|invalid|unsupported|requires|unavailable)",
            "license_mismatch": r"exception for .* lists licenses|uses disallowed licenses|unnecessary exception",
            "assertion": r"Assertion.*failed|assertion failed|panic:|FATAL ERROR:",
        }
        rows = []
        for line in data.decode(errors="replace").splitlines():
            if re.search(r"(?i)authorization|x-buildbuddy-api-key|remote_header|bes_header|github_pat_|gh[pousr]_", line):
                continue
            classes = sorted(name for name, pattern in patterns.items() if re.search(pattern, line))
            if not classes:
                continue
            rows.append(dict(classes=classes, line_sha256=sha(line.encode()),
                tools=sorted(set(re.findall(r"\b(?:aarch64-linux-gnu-gcc|aarch64-linux-gnu-g\+\+|qemu-system-x86_64|setpriv|sudo|nogo|clang|gcc)\b", line))),
                dependencies=sorted(name for name in dependencies if re.search(
                    r"(?<![A-Za-z0-9_./+-])" + re.escape(name) + r"(?![A-Za-z0-9_./+-])", line))
                    if "license_mismatch" in classes else []))
            if len(rows) == 48:
                break
        return dict(sha256=sha(data), bytes=len(data), diagnostics=rows,
                    projection_limit_reached=len(rows) == 48)

    def controls(row):
        assert project(b"Authorization: Bearer private-token")['diagnostics'] == []
        sample = project(b"execvp(/usr/bin/aarch64-linux-gnu-gcc): No such file or directory")
        assert sample['diagnostics'][0]['classes'] == ['missing_file']
        assert sample['diagnostics'][0]['tools'] == ['aarch64-linux-gnu-gcc']
        assert "private" not in json.dumps(project(b"panic: private-checkpoint"))
        dependency = sorted(dependencies)[0]
        sample = project(f"exception for {dependency} lists licenses".encode())
        assert sample['diagnostics'][0]['dependencies'] == [dependency]
        sample = project(f"exception for {dependency}-not-a-catalog-entry lists licenses".encode())
        assert dependency not in sample['diagnostics'][0]['dependencies']
        row['synthetic_projection'] = 'PASS'
        row['coverage_complete'] = True

    def invocation(row, source):
        identifier = source['invocation']
        owners = {a['owner']['label'] for a in source['suite_executed']}
        summaries = {}
        row.update(invocation=identifier, metadata_sha256=[], expected_owners=len(owners), last_test_finish=None,
                   owner_configurations=sorted({(a['owner']['label'], a['owner']['configuration']) for a in source['suite_executed']}))
        for status in sorted({{'PASSED': 5, 'FAILED': 6}[a['status']] for a in source['suite_executed']}):
            token = ''
            for _ in range(32):
                result = rpc('GetTarget', {'invocationId': identifier, 'status': status, 'pageToken': token})
                row['metadata_sha256'].append(sha(json.dumps(result, sort_keys=True).encode()))
                group, = result['targetGroups']
                assert group['status'] in (status, str(status), {5: 'PASSED', 6: 'FAILED'}[status])
                for target in group.get('targets', []):
                    label = target['metadata']['label']
                    assert label in owners and label not in summaries
                    summaries[label] = target.get('testSummary', {})
                token = group.get('nextPageToken', '')
                if not token:
                    break
            if token:
                row['partial'] = True
        row['summary_count'] = len(summaries)
        if owners and set(summaries) == owners and not row.get('partial'):
            values = []
            row['missing_summary_times'] = []
            for label, summary in summaries.items():
                value = summary.get('lastStopTime')
                if value is None:
                    if 'lastStopTimeMillis' not in summary:
                        row['missing_summary_times'].append(label)
                        continue
                    value = datetime.fromtimestamp(int(summary['lastStopTimeMillis']) / 1000, timezone.utc).isoformat()
                if isinstance(value, dict):
                    value = datetime.fromtimestamp(int(value['seconds']) + int(value.get('nanos', 0)) / 10**9, timezone.utc).isoformat()
                stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
                assert stamp.tzinfo is not None
                values.append(stamp)
            if values:
                row['last_observed_test_finish'] = max(values).isoformat()
                if not row['missing_summary_times']:
                    row['last_test_finish'] = row['last_observed_test_finish']
        row['timing_complete'] = row['last_test_finish'] is not None
        row['coverage_complete'] = row['timing_complete']

    def console(row, source):
        identifier = source['invocation']
        token = '0'; chunks = []
        for _ in range(8):
            value = json.loads(fetch('api/v1/GetLog', {'selector': {'invocation_id': identifier}, 'page_token': token}))
            chunks.append(value.get('log', {}).get('contents', ''))
            token = value.get('next_page_token', value.get('nextPageToken', ''))
            assert sum(len(c.encode()) for c in chunks) < 8 * 1024**2
            if not token:
                break
        row.update(invocation=identifier, partial=bool(token), pages=len(chunks), **project(''.join(chunks).encode()))
        row['coverage_complete'] = not token and row['bytes'] > 0
        assert row['bytes'] > 0, 'original console unavailable'

    def files(row, source, label):
        identifier = source['invocation']
        response = json.loads(fetch('api/v1/GetAction', {'selector': {'invocation_id': identifier, 'target_label': label}}))
        assert not response.get('next_page_token', response.get('nextPageToken'))
        actions = response.get('action', [])
        assert len(actions) <= 8
        selected = row['files'] = []
        row.update(invocation=identifier, owner=label, action_count=len(actions), skipped_build_rows=0)
        attempts = [a for a in source['suite_executed'] if a['owner']['label'] == label]
        matched_attempts = 0
        for action in actions:
            assert action.get('target_label', action.get('targetLabel')) == label
            identity = action['id']
            assert identity.get('invocation_id', identity.get('invocationId')) == identifier
            if attempts:
                if not any(int(action.get(key, 0)) for key in ('run', 'shard', 'attempt')):
                    row['skipped_build_rows'] += 1
                    continue
                attempt, = attempts
                assert identity.get('configuration_id', identity.get('configurationId')) == attempt['owner']['configuration']
                assert all(int(action.get(key, 0)) == attempt[key] == 1 for key in ('run', 'shard', 'attempt'))
                matched_attempts += 1
            for item in action.get('file', []):
                if item['name'] not in {'test.log', 'test.xml', 'stderr', 'stdout'}:
                    continue
                match = re.fullmatch(r'bytestream://remote\.buildbuddy\.io/(?:[^?#]*/)?blobs/([a-f0-9]{64})/(\d+)', item['uri'])
                assert match and 0 <= int(match[2]) < 2 * 1024**2
                data = blob({'hash': match[1], 'sizeBytes': match[2]}, identifier) if int(match[2]) else b''
                assert len(data) == int(match[2]) and sha(data) == match[1]
                prior = [f for f in source['original_reports'] if f['identity']['label'] == label and f['name'] == item['name']]
                if prior:
                    expected, = prior
                    assert expected['sha256'] == sha(data) and expected['bytes'] == len(data)
                selected.append(dict(name=item['name'], action_id={key: value for key, value in identity.items()
                    if key in {'invocation_id', 'invocationId', 'target_id', 'targetId',
                               'configuration_id', 'configurationId', 'action_id', 'actionId'}}, **project(data)))
        if attempts:
            assert matched_attempts == 1
        assert selected, 'no original files available for selected owner'
        row['coverage_complete'] = True

    def nogo_target(row):
        identifier = bulk['invocation']
        label = '//runsc/cgroup:cgroup_test_nogo'
        failure, = [f for f in bulk['suite_failures'] if f.get('label') == label]
        result = rpc('GetTarget', {'invocationId': identifier, 'targetLabel': label, 'status': 3})
        group, = result['targetGroups']
        target, = group['targets']
        assert not group.get('nextPageToken') and target['metadata']['label'] == label
        row.update(invocation=identifier, owner=label, configuration=failure['configuration'],
                   metadata_sha256=sha(json.dumps(result, sort_keys=True).encode()), actions=[])
        for event in target.get('actionEvents', []):
            identity = event['id']['actionCompleted']
            assert identity['label'] == label and identity['configuration']['id'] == failure['configuration']
            action = event['action']
            item = dict(exit_code=action.get('exitCode'), files=[])
            row['actions'].append(item)
            for name in ('stderr', 'stdout'):
                if name not in action:
                    continue
                match = re.fullmatch(r'bytestream://remote\.buildbuddy\.io/(?:[^?#]*/)?blobs/([a-f0-9]{64})/(\d+)', action[name].get('uri', ''))
                assert match and 0 <= int(match[2]) < 2 * 1024**2
                data = blob({'hash': match[1], 'sizeBytes': match[2]}, identifier) if int(match[2]) else b''
                assert len(data) == int(match[2]) and sha(data) == match[1]
                item['files'].append(dict(name=name, **project(data)))
        row['coverage_complete'] = bool(row['actions']) and all(a['files'] for a in row['actions'])

    phase('projection_controls', controls)
    for name, source in (('bulk', bulk), ('coretag', guest)):
        phase(name + '_timing', lambda row, s=source: invocation(row, s))
        phase(name + '_console', lambda row, s=source: console(row, s))
    for name, source, label in (
            ('license_files', bulk, '//tools/licensecheck:licensecheck_test'),
            ('coretag_files', guest, '//' + WORKER + ':guest_test')):
        phase(name, lambda row, s=source, l=label: files(row, s, l))
    phase('nogo_target', nogo_target)
    record['status'] = 'ORIGINAL_FAIL_DIAGNOSTICS_RECORDED'
    record['lookups_succeeded'] = all(p['status'] == 'PASS' for p in record['phases'].values())
    record['diagnostics_complete'] = record['lookups_succeeded'] and all(
        p.get('coverage_complete', False) for p in record['phases'].values())
    record['cause_established'] = False
    record['diagnostic_scope'] = 'Recognized log classes and exact source-known names; matching a class alone does not establish the cause'
    save()


if __name__ == "__main__":
    save()
    try:
        if sys.argv[1:] == ['--recover-original']:
            recover_original()
            sys.exit(0 if record['diagnostics_complete'] else 1)
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
