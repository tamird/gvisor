"""Check the released Bazel binary against both gVisor source trees."""

import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import time
import uuid

T = Path(os.environ["RUNNER_TEMP"]) / "bazel9"
OUT = T / "records"
STAGED = T / "staged"
ARCH = os.environ["ARCH"]
CONFIG = {"amd64": "x86_64", "arm64": "aarch64"}[ARCH]
STOCK = "aa767e4c43d011121271551fe24da501a56f729d"
PRODUCT = "542eadd05f05f309cc0d2344ac4b07e967a67fd1"
RELEASE = "//:release"
HOSTARCH = "//pkg/hostarch:hostarch_test"
PARSER = "//tools/nvidia_driver_differ:driver_ast_parser"
DEMANGLE = "@llvm-project//llvm:Demangle"
EXPECTED = {"containerd-shim-runsc-v1", "runsc", "gvisor-bin/gvisor_sentry", "gvisor-bin/checkpointgofer",
            "gvisor-bin/runsc-metric-server", "gvisor-bin/runsc-fd-parking", "gvisor-bin/gvisor-sentry-prewarmer",
            "gvisor-bin/gvisor-proc-umounter"}
report = {"stock": STOCK, "product": PRODUCT, "arch": ARCH, "phases": {}, "complete": False}


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def persist():
    (OUT / "verdict.json").write_text(json.dumps(report, indent=2) + "\n")


def checkout(sha):
    subprocess.run(["git", "checkout", "-q", "--detach", sha], check=True)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == sha
    subprocess.run(["git", "diff", "--exit-code", sha, "--"], check=True)


def phase(name, args, expect_error=None, seconds=2400, cwd=None):
    uid = str(uuid.uuid4())
    log = T / (name + ".log")
    row = report["phases"][name] = {"start": now(), "invocation": uid, "source": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()}
    persist()
    cmd, *rest = args
    location = ["-w", str(cwd)] if cwd else []
    before = time.monotonic()
    try:
        with log.open("w") as stream:
            result = subprocess.run(["docker", "exec", *location, "gnu", str(T / ("bazel-" + version)),
                                     "--bazelrc=" + str(T / "bb.rc"), cmd, "--color=no", "--curses=no", "--invocation_id=" + uid,
                                     *rest], stdout=stream, stderr=subprocess.STDOUT, timeout=seconds)
        row["exit"] = result.returncode
    except subprocess.TimeoutExpired:
        row["timeout"] = seconds
    text = log.read_text(errors="replace")
    row.update(finish=now(), seconds=round(time.monotonic()-before, 3))
    row["errors"] = [line[:600] for line in text.splitlines() if re.search(r"ERROR:|Error in |error: |fatal error:", line)][-10:]
    row["work"] = [line for line in text.splitlines() if re.search(r" processes:| processes?:|Build completed|Analyzed target",line)][-5:]
    row["ok"] = (isinstance(row.get("exit"), int) and row["exit"] != 0 and bool(re.search(expect_error, text, re.I)) if expect_error else row.get("exit") == 0)
    persist()
    print(name, row, flush=True)
    return text


def options():
    return ["--config=" + CONFIG, "--jobs=220", "--remote_download_outputs=toplevel"]


def native_files():
    STAGED.mkdir(exist_ok=True)
    subprocess.run(["docker", "exec", "gnu", "cp", "-rL", "bazel-bin/release/.", str(STAGED)], check=True)
    binary = phase("locate-test", ["cquery", *options(), "--output=files", HOSTARCH])
    rows = [line for line in binary.splitlines() if line.startswith("bazel-out/") and line.endswith("/hostarch_test")]
    path, = rows
    subprocess.run(["docker", "exec", "gnu", "cp", "-L", path, str(STAGED/"hostarch_test")], check=True)
    actual = {}
    for file in STAGED.rglob("*"):
        if file.is_file():
            data = file.read_bytes()
            assert data[:6] == b"\x7fELF\x02\x01"
            machine = struct.unpack_from("<H", data, 18)[0]
            assert machine == {"amd64": 62, "arm64": 183}[ARCH]
            actual[file.relative_to(STAGED).as_posix()] = {"machine": machine, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    assert set(actual) == EXPECTED | {"hostarch_test"}
    report["outputs"] = actual
    (STAGED/"SHA256SUMS").write_text("".join(v["sha256"] + "  " + k + "\n" for k, v in sorted(actual.items())))
    subprocess.run(["tar", "-C", str(STAGED), "-czf", str(T/"native.tgz"), "."], check=True)


def independent(name, operation):
    try:
        operation()
    except Exception as error:
        row = report["phases"].setdefault(name, {})
        row.update(ok=False, finish=now(), collection_error=f"{type(error).__name__}: {str(error)[:500]}")
        persist()


def licenses():
    label = "//tools/licensecheck/main:licensecheck"
    name = "license-" + version
    phase(name + "-build", ["build", *options(), label])
    if not report["phases"][name + "-build"]["ok"]:
        return
    output = phase(name + "-locate", ["cquery", *options(), "--output=files", label])
    paths = [line for line in output.splitlines() if line.startswith("bazel-out/") and line.endswith("/licensecheck")]
    path, = paths
    # Licensecheck invokes make mod. Use Make's normal non-Docker branch in
    # this already-provisioned builder and the exact client that built it.
    client = str(T / ("bazel-" + version))
    prefix = ["docker", "exec", "-e", "MAKEFLAGS=DOCKER_BUILD=false BAZEL=" + client, "gnu"]
    trace = subprocess.check_output([*prefix, "make", "-sn", "mod", "OPTIONS=", "TARGETS=graph --output=json"], text=True)
    assert client + " mod " in trace and "docker exec" not in trace
    row = report["phases"][name + "-verify"] = {"start": now(), "client": client, "source": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()}
    persist()
    with (T/(name+".log")).open("w") as log:
        proc = subprocess.run([*prefix, path, "--mode=verify"], stdout=log, stderr=subprocess.STDOUT, timeout=600)
    output = (T/(name+".log")).read_text(errors="replace")
    row.update(exit=proc.returncode, ok=proc.returncode == 0, finish=now(),
               diagnostics=[s[:600] for s in output.splitlines() if re.search(r"licensecheck:|missing|mismatch|version|ERROR:",s,re.I)][-15:])
    persist()


def dirwatch():
    folder = T/"watch"
    folder.mkdir()
    (folder/"MODULE.bazel").write_text('module(name="probe")\nlint_sources = use_repo_rule("//:lint_sources.bzl", "lint_sources")\nlint_sources(name="lint_inputs")\n')
    (folder/"BUILD").touch()
    shutil.copyfile(T/"lint_sources.bzl",folder/"lint_sources.bzl")
    (folder/"keep.h").touch()
    nested = folder/"newpkg/nested"
    nested.mkdir(parents=True)
    for p in (nested.parent/"BUILD.bazel", nested/"BUILD.bazel", nested/"fresh.h"):
        p.touch()
    query = ["cquery", "--output=files", "@lint_inputs//:cpplint"]
    for name, want, reject in (("new", "nested/fresh.h", None), ("deleted", "ROOT.d/keep.h", "nested/fresh.h"), ("recreated", "newpkg/other.h", "nested/fresh.h")):
        if name == "deleted":
            shutil.rmtree(nested.parent)
        if name == "recreated":
            nested.parent.mkdir()
            (nested.parent/"BUILD.bazel").touch()
            (nested.parent/"other.h").touch()
        text = phase("dirwatch-"+name,query,cwd=folder)
        row = report["phases"]["dirwatch-"+name]
        row["files"] = [s for s in text.splitlines() if re.fullmatch(r"\S*ROOT\.d/\S+\.h", s)]
        paths = "\n".join(row["files"])
        row["ok"] &= want in paths and "ROOT.d/keep.h" in paths and (reject is None or reject not in paths)
        persist()


OUT.mkdir(exist_ok=True)
persist()
try:
    checkout(STOCK)
    version = "8.8.1"
    assert Path(".bazelversion").read_text().strip() == version
    phase("stock-release-analysis", ["build", *options(), "--nobuild", RELEASE, HOSTARCH, PARSER])
    if ARCH == "amd64":
        independent("license-" + version + "-verify", licenses)
    version = "9.3.1"
    if ARCH == "amd64":
        phase("stock-coral-control", ["query", "@crosstool//:toolchains"], r"(cc_toolchain|cc_common|CcToolchainConfigInfo).*(not defined|removed|undefined)")
        phase("stock-llvm-control", ["query", DEMANGLE], r"(sh_binary|CcInfo|proto_library).*(not defined|removed|undefined)")
    checkout(PRODUCT)
    assert Path(".bazelversion").read_text().strip() == version
    if ARCH == "amd64":
        independent("dirwatch", dirwatch)
    phase("release-and-test", ["build", *options(), RELEASE, HOSTARCH], seconds=3600)
    if report["phases"]["release-and-test"]["ok"]:
        independent("release-artifacts", native_files)
    path = phase("parser-analysis", ["cquery", *options(), "somepath(" + PARSER + "," + DEMANGLE + ")"])
    report["phases"]["parser-analysis"]["ok"] &= PARSER in path and "//llvm:Demangle" in path
    persist()
    if ARCH == "amd64" and report["phases"]["parser-analysis"]["ok"]:
        phase("parser-input", ["build", *options(), DEMANGLE])
    if ARCH == "amd64":
        independent("license-" + version + "-verify", licenses)
    report["complete"] = bool(report.get("outputs")) and all(row["ok"] for row in report["phases"].values())
except Exception as err:
    report["collection_error"] = f"{type(err).__name__}: {str(err)[:500]}"
finally:
    report["verified_at"] = now()
    persist()
raise SystemExit(0 if report["complete"] else 1)
