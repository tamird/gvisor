"""Verify all members of the original archive, then restore its checkpoint."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import time

FIXED = "bea45d149b9a8e4a4bc4015c48cb6d6b6ee822fc"
STAGE = Path(os.environ["RUNNER_TEMP"]) / "release-witness"
MEMBERS = {
    "runsc": "80b13576817d3f62dae6e28baf75d164bc3105bd9f77c1b6d73f33b0b0c42e96",
    "containerd-shim-runsc-v1": "02c52bc57c42fa7e8ee939ac5b4f40565f8f820a24663794bab9f216867ff8ad",
    "gvisor-bin/checkpointgofer": "643caa6e7df1538fc435590851e1f4c43cd5d9638617ef0919313f1f754fd43c",
    "gvisor-bin/gvisor-proc-umounter": "62e0e37cdcebba6e991218e262fa08c0681ac71e4b479c45796769305c52f8aa",
    "gvisor-bin/gvisor-sentry-prewarmer": "6a1f4a9f47fd067be7d0703c4449024934548f9d9a8a68de8a237b364bb4b588",
    "gvisor-bin/gvisor_sentry": "5b5debf98736f062ffecdbb51e0c6af0101509b78a3903d7e10f459949ad997c",
    "gvisor-bin/runsc-fd-parking": "14927600c8430229d50d895e395f51f79c721ed825d9b13880e9bb0cbfc86d68",
    "gvisor-bin/runsc-metric-server": "3c956ef25960d565e4fc20349e43f3e42e9b7f2ac0d5413459a2d62920bbd120",
    "gvisor-bin/runsc-netgofer": "23908434a36c994b0ff9fd72cb48b40bfb77c3a077c66609c543893b6918730e",
}
ARCHIVE = "790e542f54eac80ab8d816a5740b5fbf6e9d81f6b58e102600d9e52c4ad99e42"
SIZE = 84396638

def file_hash(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f,"sha256").hexdigest()

def save(phase, data):
    (STAGE / (phase + ".json")).write_text(json.dumps(data,indent=2))

def install():
    archive = STAGE/"original.tar.bz2"
    assert archive.stat().st_size == SIZE and file_hash(archive) == ARCHIVE
    packaged = STAGE/"packaged"
    packaged.mkdir()
    seen = set()
    with tarfile.open(archive,"r:bz2") as tar:
        for item in tar:
            if item.isdir():
                continue
            name = item.name.removeprefix("./")
            assert name in MEMBERS and name not in seen
            assert item.isfile() and 0 < item.size < 300000000 and item.mode & 0o111
            out = packaged/name
            out.parent.mkdir(exist_ok=True)
            with tar.extractfile(item) as src, out.open("xb") as dst:
                shutil.copyfileobj(src,dst)
            assert file_hash(out) == MEMBERS[name]
            out.chmod(item.mode & 0o777)
            seen.add(name)
    assert seen == MEMBERS.keys()
    assert ("runsc version "+FIXED) in subprocess.check_output([packaged/"runsc","--version"],text=True).splitlines()


def runtime():
    install()
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
    data = {"source":FIXED, "steps":[], "archive_sha256":ARCHIVE, "archive_bytes":SIZE, "verified_members":len(MEMBERS), "cli_sha256":file_hash(runsc), "guest_sha256":file_hash(guest / "busybox")}
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
        assert label == FIXED
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
        shutil.rmtree(directory)



if __name__ == "__main__":
    runtime()
