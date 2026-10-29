"""Run the real format entry point against a disposable source checkout."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

SOURCE = "de251963efc22cdaa791b3e24d330c0c91c7e18f"
TREE = "ecb93f306ede7ef6da0b3a60aa67a184c7d2923b"
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
    result = subprocess.run(["bazel", "--bazelrc=" + os.environ["QA_RC"], command, *options, target],
                            cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
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
    (case / "BUILD").write_text("")
    other = {case / "not-a-build-file.txt": "X=2\n", case / "nested/MODULE.bazel": "",
             case / "nested/ignore.bzl": "X=3\n"}
    (case / "nested").mkdir()
    for p, contents in other.items():
        p.write_text(contents)
    bazel("new_package", "build", check_target, diagnostic=dirty.name)
    bazel("nested_fix", "run", entry, cwd=root / "tools")
    require(dirty.read_text() == clean and all(p.read_text() == data for p, data in other.items()), "wrong source scope or formatted bytes")
    require(not git("diff", "--name-only"), "formatting changed a tracked file unexpectedly: " + git("diff", "--name-only"))
    bazel("recheck", "build", check_target)
    mtime = dirty.stat().st_mtime_ns
    bazel("idempotent", "run", entry)
    require(dirty.stat().st_mtime_ns == mtime, "format rewrote an already clean source")
    config = root / ".buildifier.json"
    require(not config.exists(), "test must retain repository config")
    config.write_text("{")
    bazel("bad_config", "run", entry, diagnostic=".buildifier.json")
    config.unlink()
    dirty.write_text("x = [\n")
    bazel("invalid_source", "run", entry, diagnostic=dirty.name)
    dirty.write_text(clean)
    chroot_probe()
    require(set(report["phases"]) == set(expected), "missing phase")
    report["verdict"] = "PASS"

try:
    main()
finally:
    report["verified_at"] = now()
    save()
