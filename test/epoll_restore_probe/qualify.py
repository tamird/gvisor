"""Collect native, pre-fix, and post-fix syscall traces in a single Linux job."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback

probe, stock_runsc, fixed_runsc, evidence = map(Path, sys.argv[1:])
evidence.mkdir(parents=True, exist_ok=True)
native_trace = "live=1,1,11 mod=0,1,1,22 del=0 fresh=0,1,1,33"
before_trace = "rdev=1:8 add=0 mask=1 tag=11"
observations = {}

def read_record(path, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text().strip()
            if text:
                return text
        time.sleep(.05)
    raise TimeoutError(str(path))

def native(work):
    p = subprocess.Popen([probe, "/dev/random", work])
    try:
        before = read_record(work / "before")
        p.send_signal(signal.SIGUSR1)
        assert p.wait(timeout=20) == 0
        return {"before": before, "after": read_record(work / "after")}
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()

def container(runsc, direct, bundle, case):
    rootfs = bundle / "rootfs"
    hostwork = bundle / "work"
    image = bundle / "image"
    for directory in (rootfs / "work", rootfs / "devices", hostwork, image):
        directory.mkdir(parents=True)
    (rootfs / "devices/host-random").touch()
    shutil.copy2(probe, rootfs / "probe")
    mounts = [
        {"type": "bind", "source": "/dev/random", "destination": "/devices/host-random",
         "options": ["rbind", "rw"]},
        {"type": "bind", "source": str(hostwork), "destination": "/work",
         "options": ["rbind", "rw"]},
    ]
    spec = {
        "ociVersion": "1.0.2", "root": {"path": "rootfs"},
        "process": {"terminal": False, "user": {"uid": 0, "gid": 0}, "cwd": "/",
                    "args": ["/probe", "/devices/host-random", "/work"], "env": ["PATH=/"]},
        "mounts": mounts,
        "linux": {"namespaces": [{"type": t} for t in ("pid", "mount", "ipc", "uts", "network")]},
    }
    (bundle / "config.json").write_text(json.dumps(spec))
    log_path = evidence / (case + ".log")
    debug_dir = bundle / "debug"
    debug_dir.mkdir()
    root = bundle / "state"
    root.mkdir()
    command = [runsc, "--root=" + str(root), "--platform=systrap", "--network=none",
               "--overlay2=none", "--character-device-policy=passthrough",
               "--directfs=" + str(direct).lower(), "--host-settings=ignore",
               "--ignore-cgroups", "--debug", "--debug-log=" + str(debug_dir) + "/"]
    patches = {}
    with log_path.open("w") as log:
        def call(args, *, check=True):
            print("$ runsc", *args, file=log, flush=True)
            return subprocess.run(command + args, stdin=subprocess.DEVNULL, stdout=log,
                                  stderr=subprocess.STDOUT, check=check, timeout=90)
        cid = "gofer-test"
        try:
            call(["run", "--detach", "--bundle=" + str(bundle), cid])
            patches["before"] = read_record(hostwork / "before")
            call(["checkpoint", "--image-path=" + str(image), cid])
            patches["checkpoint"] = {p.name: p.stat().st_size for p in image.iterdir()}
            assert any(p.stat().st_size > 0 for p in image.iterdir()), patches
            call(["delete", "--force", cid])
            call(["restore", "--detach", "--bundle=" + str(bundle),
                  "--image-path=" + str(image), cid])
            call(["kill", cid, "USR1"])
            patches["after"] = read_record(hostwork / "after")
            call(["wait", cid])
            return patches
        finally:
            try:
                call(["delete", "--force", cid], check=False)
            finally:
                # Match specutils.UnmountNullNetNS: this mount survives delete.
                while subprocess.run(["umount", "--lazy", root / "null-netns"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                    pass
                shutil.copytree(debug_dir, evidence / (case + "-debug"))

with tempfile.TemporaryDirectory(prefix="gpr-", dir="/tmp") as tmp:
    work = Path(tmp)
    try:
        observations["linux"] = native(work)
    except Exception:
        observations["linux"] = {"error": traceback.format_exc()}
    for impl, runsc in (("stock", stock_runsc), ("fixed", fixed_runsc)):
        for direct in (False, True):
            case = impl + ("-directfs" if direct else "-lisafs")
            path = work / case
            path.mkdir()
            try:
                observations[case] = container(runsc, direct, path, case)
            except Exception:
                observations[case] = {"error": traceback.format_exc()}
            print(case, observations[case], flush=True)
results = {}
for case, got in observations.items():
    if case.startswith("stock"):
        # Classify only the demonstrated defect, retaining all independent MOD/DEL observations.
        results[case] = got.get("before") == before_trace and " fresh=1,0,0,0" in got.get("after", "")
    else:
        results[case] = got.get("before") == before_trace and got.get("after") == native_trace
record = {"verdict": "PASS" if len(results) == 5 and all(results.values()) else "FAIL",
          "sources": {"original": "e82a0e179", "corrected": "64758ff8b",
                      "originalWithProbe": "039367497d", "correctedWithProbe": "b5b6ac4ac4"},
          "kernel": os.uname().release, "expected": ["linux", "stock-lisafs", "stock-directfs",
                       "fixed-lisafs", "fixed-directfs"], "observations": observations,
          "checks": results}
(evidence / "verdict.json").write_text(json.dumps(record, indent=2) + "\n")
print(json.dumps(record, indent=2), flush=True)
sys.exit(record["verdict"] != "PASS")
