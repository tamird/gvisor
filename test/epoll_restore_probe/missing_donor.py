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

def container(runsc, direct, bundle, case, drop_watch):
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
    if drop_watch:
        spec["process"]["args"].append("drop-watch")
    (bundle / "config.json").write_text(json.dumps(spec))
    log_path = evidence / (case + ".log")
    debug_dir = bundle / "debug"
    debug_dir.mkdir()
    root = bundle / "state"
    root.mkdir()
    command = [runsc, "--root=" + str(root), "--platform=systrap", "--network=none",
               "--overlay2=none", "--character-device-policy=passthrough",
               "--directfs=" + str(direct).lower(), "--host-settings=ignore",
               "--ignore-cgroups", "--debug-log=" + str(debug_dir) + "/"]
    patches = {}
    with log_path.open("w") as log:
        def call(args, *, check=True):
            print("$ runsc", *args, file=log, flush=True)
            opts = ["--debug"] if args[0] == "restore" else []
            return subprocess.run(command + opts + args, stdin=subprocess.DEVNULL, stdout=log,
                                  stderr=subprocess.STDOUT, check=check, timeout=90)
        cid = "gofer-test"
        try:
            call(["run", "--detach", "--bundle=" + str(bundle), cid])
            patches["before"] = read_record(hostwork / "before")
            call(["checkpoint", "--image-path=" + str(image), cid])
            patches["checkpoint"] = {p.name: p.stat().st_size for p in image.iterdir()}
            assert any(p.stat().st_size > 0 for p in image.iterdir()), patches
            call(["delete", "--force", cid])
            status = call(["restore", "--detach", "--bundle=" + str(bundle),
                           "--image-path=" + str(image), cid], check=False).returncode
            patches["restore_status"] = status
            if status:
                return patches
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


with tempfile.TemporaryDirectory(prefix="gpd-", dir="/tmp") as tmp:
    work = Path(tmp)
    for name, runsc, drop in (("old-watch", stock_runsc, False),
                              ("fixed-watch", fixed_runsc, False),
                              ("fixed-empty", fixed_runsc, True)):
        bundle = work / name
        bundle.mkdir()
        try:
            observations[name] = container(runsc, False, bundle, name, drop)
        except Exception:
            observations[name] = {"error": traceback.format_exc()}
        logs = [evidence / (name + ".log"), *(evidence / (name + "-debug")).glob("*")]
        out = "\n".join(p.read_text(errors="replace") for p in logs if p.is_file())
        observations[name]["omitted"] = "TEST_ONLY omitted host device donor" in out
        observations[name]["guard_message"] = "gofer did not donate a host file descriptor" in out
        print(name, observations[name], flush=True)
old, corrected, empty = (observations[key] for key in ("old-watch", "fixed-watch", "fixed-empty"))
checks = {
    "old-watch": old.get("before") == before_trace and old.get("restore_status") == 0
                 and "fresh=1,0,0,0" in old.get("after", ""),
    "fixed-watch": corrected.get("before") == before_trace and
                   corrected.get("restore_status", 0) != 0 and corrected["guard_message"],
    "fixed-empty": empty.get("before") == empty.get("after") == "rdev=1:8 no watch"
                   and empty.get("restore_status") == 0,
}
checks = {k: passed and observations[k]["omitted"] for k, passed in checks.items()}
record = {"verdict": "PASS" if all(checks.values()) else "FAIL", "checks": checks,
          "expected": list(checks), "observations": observations,
          "sources": {"original": "64758ff8b", "changed": "d971b7abc"},
          "kernel": os.uname().release}
(evidence / "verdict.json").write_text(json.dumps(record, indent=2) + "\n")
print(json.dumps(record, indent=2))
sys.exit(record["verdict"] != "PASS")
