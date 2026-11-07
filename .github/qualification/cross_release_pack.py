"""Package the reduced AMD64 builder's ARM64 Bazel release for native execution."""

import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import tarfile

SOURCE = "c115840f8a85010111fa5d72b611d7d9ab1df34b"
COMPILER = "4bee07965270dc7cb4ed89eb6a3b17968b3e0a90"
BEFORE = {
    "runsc": "86abcf70c194aad822e21e96a1e1ed61d8082924c794be6205499d7733a8322c",
    "gvisor-bin/gvisor_sentry": "1e39789a667dfd8691853b37cdafcc36b5ad2f0534c6116141a0cb0791a2c41f",
}
MEMBERS = {"runsc", "containerd-shim-runsc-v1"} | {"gvisor-bin/" + sidecar for sidecar in (
    "gvisor_sentry", "checkpointgofer", "gvisor-sentry-prewarmer", "runsc-fd-parking",
    "runsc-metric-server", "runsc-netgofer", "gvisor-proc-umounter")}
TMP = Path(os.environ["RUNNER_TEMP"])
HANDOFF = TMP / "handoff"
AUDIT = TMP / "runtime-cross/verdict.json"
BZ = ["/usr/local/bin/bazel", "--nosystem_rc", "--nohome_rc"]
FLAGS = ["--config=aarch64", "--jobs=4", "--remote_cache=", "--remote_executor=", "--disk_cache="]
RECEIPT = {"source": SOURCE, "compiler": COMPILER, "verified": False}


def save():
    (TMP / "pack.json").write_text(json.dumps(RECEIPT, indent=2) + "\n")


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def digest(path):
    with path.open("rb") as stream:
        h = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pack():
    assert output("git", "rev-parse", "HEAD") == SOURCE and output("git", "rev-parse", "HEAD^") == COMPILER
    assert platform.machine() == "x86_64"
    result = json.loads(AUDIT.read_text())
    assert result["verified"] and result["source"] == SOURCE and result["host"] == "x86_64"
    assert result["native_gcc"] == "x86_64-linux-gnu" and result["build_essential"].startswith("installed ")
    assert all(v is None for v in result["foreign_tools"].values()) and all(v is None for v in result["foreign_packages"].values())
    arm = result["arm64"]
    assert arm["flags"] == FLAGS and arm["build_exit"] == 0 and arm["declared_llvm_consumer_actions_verified"]
    targets = {"//runsc:runsc": "runsc", "//runsc/cmd/sentry:gvisor_sentry": "gvisor-bin/gvisor_sentry"}
    direct = {targets[b["owner"]]: b["sha256"] for b in arm["binaries"]
              if b["machine"] == 183 and not b["interpreter"] and not b["dynamic_segment"] and b["fresh_link"]}
    assert direct.keys() == BEFORE.keys()
    RECEIPT.update(phase="build //:release", nativeHost="x86_64", target="aarch64", builder=result["image_id"],
                   producerAuditSHA256=digest(AUDIT), direct=direct, matchesPrior={p: h == BEFORE[p] for p, h in direct.items()})
    save()
    with (TMP / "release-build.log").open("wb") as log:
        cmd = [*BZ, "build", *FLAGS, "--noshow_progress", "--color=no", "--curses=no", "//:release"]
        status = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT).returncode
    RECEIPT["buildExit"] = status
    save()
    assert status == 0
    execroot = Path(output(*BZ, "info", "execution_root"))
    outputs = output(*BZ, "cquery", *FLAGS, "--output=files", "//:release").splitlines()
    paths = {}
    for path in outputs:
        prefix, sep, member = path.partition("/bin/release/")
        assert sep and prefix.startswith("bazel-out/aarch64-") and member in MEMBERS and member not in paths
        paths[member] = execroot / path
    assert paths.keys() == MEMBERS
    HANDOFF.mkdir()
    archive = HANDOFF / "release.tar.gz"
    files = {}
    with tarfile.open(archive, "w:gz", compresslevel=1) as tar:
        for name, path in sorted(paths.items()):
            info = tarfile.TarInfo(name)
            info.size = path.stat().st_size
            info.mode = 0o755
            assert 0 < info.size < 160 << 20 and os.access(path, os.X_OK)
            with path.open("rb") as data:
                header = data.read(64)
                assert header[:6] == b"\x7fELF\x02\x01" and int.from_bytes(header[18:20], "little") == 183
                data.seek(0)
                tar.addfile(info, data)
            files[name] = {"sha256": digest(path), "bytes": info.size}
        assert all(files[n]["sha256"] == h for n, h in direct.items())
    assert archive.stat().st_size < 200 << 20 and sum(f["bytes"] for f in files.values()) < 400 << 20
    manifest = {"source": SOURCE, "compiler": COMPILER, "nativeHost": "x86_64", "target": "aarch64",
                "builder": result["image_id"], "direct": direct, "files": files, "matchesPrior": RECEIPT["matchesPrior"],
                "archiveSHA256": digest(archive), "archiveBytes": archive.stat().st_size,
                "producerAuditSHA256": digest(AUDIT)}
    meta = HANDOFF / "manifest.json"
    meta.write_text(json.dumps(manifest, sort_keys=True) + "\n")
    RECEIPT.update(phase="complete", files=files, manifestSHA256=digest(meta), archiveSHA256=manifest["archiveSHA256"],
                   archiveBytes=manifest["archiveBytes"], verified=True)
    save()


try:
    pack()
except (AssertionError, KeyError, ValueError, OSError, subprocess.SubprocessError) as exc:
    RECEIPT["errorType"] = type(exc).__name__
    save()
    raise
