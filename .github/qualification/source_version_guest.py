"""Verify an installation from exported sources, then restore its checkpoint."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

SOURCE = "111c72d26660563b1cf23993293c4aa6fbdec851"
STAGE = Path(os.environ["RELEASE_DIR"])
FIXED = os.environ["RELEASE_LABEL"]


def file_hash(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def save(phase, data):
    (STAGE / (phase + ".json")).write_text(json.dumps(data, indent=2))


def verify_files():
    root = STAGE / "packaged"
    files = {p.relative_to(root).as_posix(): p for p in root.rglob("*") if p.is_file()}
    assert set(files) == set(NAMES), files.keys()
    for p in files.values():
        with p.open("rb") as stream:
            header = stream.read(20)
        assert header[:6] == bytes.fromhex("7f454c460201") and int.from_bytes(header[18:], "little") == 62 and os.access(p, os.X_OK)
    for name in ("runsc", "gvisor-bin/runsc-netgofer"):
        assert subprocess.check_output([files[name], "--version"], text=True).splitlines()[0] == "runsc version " + FIXED, name
    if FIXED != "(devel)":
        for expected in (FIXED, "release-other"):
            run = subprocess.run([files["gvisor-bin/runsc-netgofer"], "--platform=systrap", "flags"],
                                 env={**os.environ, "GVISOR_ENFORCE_RELEASE": expected}, capture_output=True)
            if expected == FIXED:
                assert run.returncode == 0
            else:
                assert run.returncode and b"does not match the expected release" in (run.stdout + run.stderr)
    return {name: file_hash(p) for name, p in files.items()}


NAMES = ('containerd-shim-runsc-v1', 'gvisor-bin/checkpointgofer', 'gvisor-bin/gvisor-proc-umounter', 'gvisor-bin/gvisor-sentry-prewarmer', 'gvisor-bin/gvisor_sentry', 'gvisor-bin/runsc-fd-parking', 'gvisor-bin/runsc-metric-server', 'gvisor-bin/runsc-netgofer', 'runsc')

def runtime():
    delivery = STAGE
    runsc = delivery / "packaged/runsc"
    directory = STAGE / "checkpoint-witness"
    directory.mkdir()
    for sub in ("bundle/rootfs", "runtime", "image"):
        (directory / sub).mkdir(parents=True)
    bundle = directory / "bundle"
    guest = bundle / "rootfs"
    shutil.copy2(delivery / "guest-busybox", guest / "busybox")
    (guest / "hold").touch()
    spec = {"ociVersion":"1.0.2", "root":{"path":"rootfs"},
            "process":{"terminal":False,"user":{"uid":0,"gid":0},"cwd":"/","args":["/busybox","tail","-f","/hold"],"env":["PATH=/"]},
            "mounts":[{"type":"proc","source":"proc","destination":"/proc"}],
            "linux":{"namespaces":[{"type":t} for t in ("pid","mount","ipc","uts","network")]}}
    (bundle / "config.json").write_text(json.dumps(spec))
    data = {"source":SOURCE, "display_version":FIXED, "steps":[], "file_sha256":verify_files(), "cli_sha256":file_hash(runsc), "guest_sha256":file_hash(guest / "busybox")}
    def persist():
        save("runtime",data)
        (STAGE / "runtime.json").chmod(0o644)
    def call(command, *args, required=True):
        step = {"command":command}
        data["steps"].append(step)
        persist()
        started = time.monotonic()
        # The detached sandbox inherits the guest's stdout and stderr. Files
        # let the caller wait for runsc itself without waiting for pipe EOF.
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                proc = subprocess.run([runsc,"--root="+str(directory/"runtime"),"--platform=systrap","--network=none",
                    "--overlay2=none","--host-settings=ignore","--ignore-cgroups","--state-engine=graph",
                    command,*args], stdout=out, stderr=err, stdin=subprocess.DEVNULL, timeout=110)
                step["exit"] = proc.returncode
            except subprocess.TimeoutExpired:
                step["timeout"] = True
                raise
            finally:
                step["milliseconds"] = round(1000 * (time.monotonic() - started))
                out.seek(0)
                err.seek(0)
                stdout, stderr = out.read(65536), err.read(65536)
                markers = ("permission denied", "operation not permitted", "does not match across checkpoint restore",
                           "no such file or directory", "failed to start", "unknown flag", "cannot allocate",
                           "no space left", "invalid argument", "not a directory", "unknown command")
                step["stderr_categories"] = [m for m in markers if m.encode() in stderr.lower()]
                step["stderr_sha256"] = hashlib.sha256(stderr).hexdigest()
                persist()
        if required:
            assert step["exit"] == 0, command
        return stdout
    cid = "clean-release-identity"
    try:
        call("run","--detach","--bundle="+str(bundle),cid)
        assert json.loads(call("state",cid))["status"] == "running"
        call("checkpoint","--image-path="+str(directory/"image"),cid)
        with (directory / "image/checkpoint.img").open("rb") as checkpoint:
            assert checkpoint.read(8) == b"gVisorSF"
            length = int.from_bytes(checkpoint.read(8), "big")
            assert 0 < length < 16*1024*1024
            label = json.loads(checkpoint.read(length))["runsc_version"]
        if FIXED == "(devel)":
            assert label.startswith("go-buildid:") and label not in ("go-buildid:", "go-buildid:redacted"), label
        else:
            assert label == FIXED, label
        data["metadata_identity"] = label
        persist()
        call("delete","--force",cid)
        call("restore","--detach","--bundle="+str(bundle),"--image-path="+str(directory/"image"),cid)
        assert json.loads(call("state",cid))["status"] == "running"
        assert call("exec",cid,"/busybox","echo","checkpoint-restored").strip() == b"checkpoint-restored"
        data["restored_guest"] = True
    finally:
        call("delete","--force",cid,required=False)
        persist()
        # runsc reuses this bind mount for every guest under --root.
        # specutils.UnmountNullNetNS releases each stacked mount before
        # gVisor's own tests remove their temporary runtime root.
        mount = directory / "runtime/null-netns"
        count = 0
        if mount.exists():
            while True:
                rc = subprocess.run(["mountpoint", "-q", "--", mount]).returncode
                if rc == 32:
                    break
                if rc != 0:
                    raise RuntimeError(f"mountpoint exited {rc}")
                subprocess.run(["umount", "--lazy", "--", mount], check=True)
                count += 1
        data["null_netns_unmounts"] = count
        shutil.rmtree(directory)
        data["removed_runtime_root"] = not directory.exists()
        persist()



if __name__ == "__main__":
    runtime()
