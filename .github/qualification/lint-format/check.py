"""Run the real format entry point against a disposable source checkout."""
from datetime import datetime, timezone
from hashlib import sha256
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

SOURCE = "1d96ea04c417f23fd7d90abb453e93163aa83c1a"
TREE = "9450c6a36fd1b887f0e0e3b73df00343a46aa3e7"
SUPPLIER = "887377fc64d23a850f4d18a077b5db05b19913f4b99b270d193f3c7334b5a9a7"
IMAGE = "docker://gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a"
out = Path(os.environ["RUNNER_TEMP"])
root = Path.cwd()
report = dict(verdict="INCOMPLETE", phases={})
entry = "//tools/lint:format"
check_target = "//tools/lint:buildifier"
expected = ["clean", "new_package", "nested_fix", "recheck", "idempotent", "bad_config", "invalid_source", "scratch"]
report["expected"] = expected

def now():
    return datetime.now(timezone.utc).isoformat()

def save():
    (out / "workspace-format.json").write_text(json.dumps(report, indent=2) + "\n")

def require(ok, message):
    if not ok:
        raise RuntimeError(message)

def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()

def bazel(phase, command, target, cwd=root, diagnostic=None):
    invocation = str(uuid.uuid4())
    row = dict(invocation=invocation, target=target, started=now(), expected_success=diagnostic is None)
    report["phases"][phase] = row
    save()
    options = ["--config=x86_64", "--jobs=24", "--remote_download_outputs=all", "--remote_default_exec_properties=OSFamily=linux",
               "--remote_default_exec_properties=Arch=amd64", "--remote_default_exec_properties=EstimatedMemory=2GB",
               "--remote_default_exec_properties=workload-isolation-type=firecracker", "--remote_default_exec_properties=container-image=" + IMAGE,
               "--invocation_id=" + invocation]
    if phase == "nested_fix":
        bep = out / "format-exec-request.json"
        options.append("--build_event_json_file=" + str(bep))
    result = subprocess.run(["bazel", "--bazelrc=" + os.environ["QA_RC"], command, *options, target],
                            cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if phase == "nested_fix":
        # Retain only the actual launcher, its argv flag, and whether the
        # formatter was the delegated target. Never persist the run environment.
        requests = [e["execRequest"] for e in map(json.loads, bep.read_text().splitlines()) if "execRequest" in e]
        bep.unlink()
        require(len(requests) == 1, "missing actual Bazel run request")
        argv = [base64.b64decode(a).decode() for a in requests[0]["argv"]]
        row["client_exec"] = dict(argv0=argv[0], argv1=argv[1], target_invoked="format_/format" in argv[-1], executes=requests[0].get("shouldExec"))
        require(requests[0].get("shouldExec") and "format_/format" in argv[-1], "unexpected default Bazel run ExecRequest")
    row.update(exit=result.returncode, finished=now())
    # Never persist the raw startup script or the remote headers.
    print(phase, target, row["exit"], invocation, flush=True)
    if result.returncode:
        errors = [line[:330] for line in result.stdout.splitlines() if
                  ("ERROR:" in line or "failed" in line or "error" in line.lower() or "syntax" in line or "reformat" in line) and
                  not any(term in line.lower() for term in ("header", "api-key", "credential", "token", "authorization", "running command line"))]
        row["diagnostics"] = errors[-5:]
        print("\n".join(row["diagnostics"]), flush=True)
    row["matched"] = diagnostic is None or diagnostic in result.stdout
    save()
    require(row["matched"] and ((result.returncode == 0) == (diagnostic is None)), phase + " did not meet expectations")

def chroot_probe():
    runner = root / "bazel-bin/tools/lint/format_/format"
    require(runner.is_file(), "missing native format launcher")
    manifest = Path(str(runner) + ".runfiles/MANIFEST")
    require(manifest.is_file(), "missing Bazel runfiles")
    supplier = None
    for line in manifest.read_text().splitlines():
        logical, _, actual = line.partition(" ")
        if "buildifier" not in logical or not actual:
            continue
        p = Path(actual)
        if p.is_file() and sha256(p.read_bytes()).hexdigest() == SUPPLIER:
            supplier = p
            break
    require(supplier is not None, "pinned native Buildifier is not in the formatter's runfiles")
    for binary in [runner, supplier]:
        require("INTERP" not in subprocess.check_output(["readelf", "-lW", binary], text=True), "dynamic binary: " + str(binary))
    scratch = out / "workspace-format-root"
    for d in ["src", "runfiles/supplier"]:
        (scratch / d).mkdir(parents=True)
    shutil.copy(runner, scratch / "workspace-command")
    shutil.copy(supplier, scratch / "runfiles/supplier/buildifier")
    fixture = scratch / "src/BUILD"
    fixture.write_text('filegroup(name="ok")\n')
    files = ["workspace-command", "runfiles/supplier/buildifier", "src/BUILD"]
    require(sorted(p.relative_to(scratch).as_posix() for p in scratch.rglob("*") if p.is_file()) == sorted(files), "unintended scratch tools")
    subprocess.run(["sudo", "env", "-i", "PATH=/nonexistent", "RUNFILES_DIR=/runfiles", "BUILD_WORKSPACE_DIRECTORY=/src",
                    "/usr/sbin/chroot", scratch, "/workspace-command", "supplier/buildifier", "--config=off",
                    "--mode=fix", "--", "BUILD"], check=True)
    require(fixture.read_text() == 'filegroup(name = "ok")\n', "incorrect scratch output")
    report["phases"]["scratch"] = dict(files=files, supplier=SUPPLIER, original='filegroup(name="ok")', formatted=fixture.read_text().strip())

def main():
    report.update(source=git("rev-parse", "HEAD"), tree=git("rev-parse", "HEAD^{tree}"))
    require(report["source"] == SOURCE and report["tree"] == TREE and not git("diff", "--name-only"), "wrong source checkout")
    bazel("clean", "build", check_target)
    case = root / "pkg" / ".format fixture"
    case.mkdir()
    dirty = case / "dollar $ and 'quote.bzl"
    clean = '"""A fixture."""\n\nX = 1\n'
    dirty.write_text(clean.replace("X = 1", "X=1"))
    build = case / "BUILD.bazel"
    formatted_build = 'filegroup(name = "example")\n'
    malformed_build = 'filegroup(name="example")\n'
    build.write_text(malformed_build)
    other = {case / "not-a-build-file.txt": "X=2\n", case / "nested/MODULE.bazel": "",
             case / "nested/ignore.bzl": "X=3\n"}
    (case / "nested").mkdir()
    for p, contents in other.items():
        p.write_text(contents)
    before = {p: p.read_bytes() for p in (build, dirty, *other)}
    bazel("new_package", "build", check_target, diagnostic=build.relative_to(root).as_posix() + " # reformat")
    require(all(p.read_bytes() == b for p, b in before.items()), "read-only check modified a source")
    require(not git("diff", "--name-only"), "read-only check changed tracked files")
    bazel("nested_fix", "run", entry, cwd=root / "tools")
    require(build.read_text() == formatted_build and dirty.read_text() == clean and all(p.read_text() == data for p, data in other.items()), "wrong source scope or formatted bytes")
    require(not git("diff", "--name-only"), "formatting changed a tracked file unexpectedly: " + git("diff", "--name-only"))
    bazel("recheck", "build", check_target)
    mtimes = {p: p.stat().st_mtime_ns for p in (dirty, build)}
    bazel("idempotent", "run", entry)
    require(all(p.stat().st_mtime_ns == mtime for p, mtime in mtimes.items()), "format rewrote an already clean source")
    config = root / ".buildifier.json"
    require(not config.exists(), "test must retain repository config")
    config.write_text("{")
    bazel("bad_config", "run", entry, diagnostic=".buildifier.json")
    config.unlink()
    dirty.write_text("x = [\n")
    bazel("invalid_source", "run", entry, diagnostic=dirty.name)
    dirty.write_text(clean)
    chroot_probe()
    report["controls"] = dict(new_build=build.relative_to(root).as_posix(), invalid=malformed_build, formatted=formatted_build, check_preserved_inputs=True, ignored_nested_workspace=True)
    require(set(report["phases"]) == set(expected), "missing phase")
    report["verdict"] = "PASS"

try:
    main()
finally:
    report["verified_at"] = now()
    save()
