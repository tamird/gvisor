"""Build the archive exactly as described in the Linux installation guide."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import traceback

import runtime_guest as guest

MODE = os.environ["SOURCE_MODE"]
SOURCE = {"stock": "1f49f256eae8d2dd2c69ea4cfe9ef2a5bb05516d", "proposal": "e80c8fde8e8b8bb4352f28c321f9d2ec76977159"}[MODE]
TARGET = "//debian:gvisor-release-tar-bz2"
ARCHIVE = Path("bazel-bin/debian/gvisor.tar.bz2")
OUT = Path(os.environ["RUNNER_TEMP"]) / "release-rbe" / MODE
OUT.mkdir()
guest.q.TASK = guest.r.TASK = OUT
MEMBERS = {"runsc", "containerd-shim-runsc-v1"} | {"gvisor-bin/" + p for p in (
    "checkpointgofer", "gvisor_sentry", "gvisor-sentry-prewarmer", "runsc-fd-parking",
    "runsc-metric-server", "runsc-netgofer", "gvisor-proc-umounter",
)}


def write(result):
    (OUT / "install.json").write_text(json.dumps(result, indent=2) + "\n")


def main(result):
    assert platform.machine() == "aarch64" and os.getuid() != 0
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    command = ["bazelisk", "build", "-c", "opt", TARGET]
    readme = Path("README.md").read_text()
    if MODE == "proposal":
        assert "\n"+" ".join(command)+"\n" in readme
        assert "\nsudo tar -C /usr/local/bin -xf "+str(ARCHIVE)+"\n" in readme
    else:
        assert "\nbazel build -c opt "+TARGET+"\n" in readme
    result["host_compilers"] = {name:bool(shutil.which(name)) for name in ("gcc", "x86_64-linux-gnu-gcc", "aarch64-linux-gnu-gcc")}
    version = subprocess.check_output(["bazelisk", "--version"], text=True).strip()
    assert version == "bazel 9.3.1"
    result.update(phase="build", bazel=version, free_before=shutil.disk_usage(OUT).free)
    write(result)
    rc, stdout, stderr = guest.q.command(command, 2000 if MODE == "stock" else 4600)
    result.update(exit=rc, free_after=shutil.disk_usage(OUT).free, diagnostics=guest.q.diagnostics(stderr))
    if rc != 0:
        result["output"] = guest.q.diagnostics(stdout)
    write(result)
    if rc != 0 and MODE == "stock":
        result["status"] = "STOCK_BUILD_FAILED"
        return
    assert rc == 0
    result["phase"] = "unpack"
    files = result["files"] = {}
    result["archive_sha256"] = hashlib.sha256(ARCHIVE.read_bytes()).hexdigest()
    install = OUT / "installation"
    install.mkdir()
    with tarfile.open(ARCHIVE, "r:bz2") as tf:
        for member in tf:
            name = member.name.removeprefix("./").rstrip("/")
            if member.isdir():
                assert name in (".", "", "gvisor-bin")
                continue
            assert name in MEMBERS and name not in files and member.isfile() and 0 < member.size < 250*1024**2
            assert member.mode & 0o7777 == 0o755
            raw = tf.extractfile(member).read()
            assert raw[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<H",raw,18)[0] == 183
            files[name] = {"size": member.size, "mode": member.mode, "sha256": hashlib.sha256(raw).hexdigest()}
    assert set(files) == MEMBERS
    subprocess.run(["tar", "-C", install, "-xf", ARCHIVE], check=True)
    for name, meta in files.items():
        path = install / name
        assert stat.S_IMODE(path.stat().st_mode) == 0o755 and not path.is_symlink()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == meta["sha256"]
    result["phase"] = "guest"
    guest.r.SOURCE = SOURCE
    guest.MEMBERS = MEMBERS
    busybox = guest.fixture(result.setdefault("fixture", {}), "arm64")
    capsule, marker = guest.capsule("guest-root", busybox, install)
    smoke = result["smoke"] = {}
    output = guest.launch(capsule, smoke)
    smoke["marker"] = output.splitlines() == [marker.encode()]
    assert smoke["exit"] == 0 and all(smoke[k] for k in ("marker", "private_root_entered", "sentry_selected", "sentry_release_matches", "sentry_stdio_witness"))
    result["status"] = "PASS"


result = {"source":SOURCE, "phase":"source", "status":"INCOMPLETE"}
try:
    main(result)
except (AssertionError, KeyError, ValueError, OSError, subprocess.SubprocessError, tarfile.TarError) as failure:
    result.update(status="FAIL", error=type(failure).__name__, line=traceback.extract_tb(failure.__traceback__)[-1].lineno)
finally:
    write(result)
sys.exit(result["status"] not in ("PASS", "STOCK_BUILD_FAILED"))
