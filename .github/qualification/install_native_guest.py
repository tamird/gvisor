"""Install the archive just produced by the ARM64 RBE worker and boot it on AMD64."""
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import struct
import subprocess
import tarfile
import traceback

import runtime_guest as guest

ROOT = Path(os.environ["RUNNER_TEMP"]) / "release-rbe"
REPORT = ROOT / "public-tar.json"
ARCHIVE = Path("bazel-bin/debian/gvisor.tar.bz2")
SOURCE = "bbbed5b2b84fe21597c740789d841e618f912def"
MEMBERS = {"runsc", "containerd-shim-runsc-v1"} | {"gvisor-bin/" + p for p in (
    "checkpointgofer", "gvisor_sentry", "gvisor-sentry-prewarmer", "runsc-fd-parking",
    "runsc-metric-server", "runsc-netgofer", "gvisor-proc-umounter",
)}

def verify(row, smoke):
    assert row["source"] == SOURCE and row["bazel_exit"] == 0
    assert row["coordinator"] == platform.machine() == "x86_64" and os.getuid() != 0
    assert not (ROOT / "private.bazelrc").exists()
    assert not any(k in os.environ for k in ("ACCESS", "BUILDBUDDY_API_KEY", "GITHUB_TOKEN", "GH_TOKEN"))
    assert set(row["files"]) == set(row["input_files"]) == MEMBERS
    digest = hashlib.sha256(ARCHIVE.read_bytes()).hexdigest()
    assert digest == row["archive_digest"] and ARCHIVE.stat().st_size == row["archive_size"]
    smoke.update(archive_sha256=digest, native_arch=platform.machine(), platform="systrap", network="none", rootless=True)
    with tarfile.open(ARCHIVE, "r:bz2") as tf:
        names = set()
        for m in tf:
            name = m.name.removeprefix("./").rstrip("/")
            if m.isdir():
                assert name in ("", ".", "gvisor-bin")
                continue
            assert name in MEMBERS and name not in names and m.isfile()
            assert m.mode & 0o7777 == 0o755 and m.size == row["files"][name]["bytes"]
            names.add(name)
        assert names == MEMBERS
    work = ROOT / "native-install"
    work.mkdir()
    install = work / "install"
    install.mkdir()
    subprocess.run(["tar", "-C", install, "-xf", ARCHIVE], check=True)
    smoke["installed"] = {}
    for name in sorted(MEMBERS):
        path = install / name
        assert not path.is_symlink() and stat.S_IMODE(path.stat().st_mode) == 0o755
        raw = path.read_bytes()
        assert raw[:6] == b"\x7fELF\x02\x01" and struct.unpack_from("<H", raw, 18)[0] == 62
        sha = hashlib.sha256(raw).hexdigest()
        assert sha == row["files"][name]["sha256"] == row["input_files"][name]["sha256"]
        smoke["installed"][name] = sha
    guest.q.TASK = guest.r.TASK = work
    guest.r.SOURCE = SOURCE
    guest.MEMBERS = MEMBERS
    busybox = guest.fixture(smoke.setdefault("fixture", {}), "amd64")
    capsule, marker = guest.capsule("guest-root", busybox, install)
    output = guest.launch(capsule, smoke)
    smoke["marker"] = output.splitlines() == [marker.encode()]
    assert smoke["exit"] == 0 and all(smoke[k] for k in ("marker", "private_root_entered", "sentry_selected", "sentry_release_matches", "sentry_stdio_witness"))

row = json.loads(REPORT.read_bytes())
if row["status"] == "PASS":
    smoke = row["guest"] = {"status": "INCOMPLETE"}
    try:
        verify(row, smoke)
        smoke["status"] = "PASS"
    except Exception as failure:
        smoke.update(error=type(failure).__name__, line=traceback.extract_tb(failure.__traceback__)[-1].lineno)
    finally:
        REPORT.write_text(json.dumps(row, indent=2) + "\n")
    if smoke["status"] != "PASS":
        raise SystemExit(1)
