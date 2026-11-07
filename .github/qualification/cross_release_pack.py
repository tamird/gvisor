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
PRIOR_RELEASE = {
    "containerd-shim-runsc-v1": "56cb980dd21bff565d463a4cf118ca40222a8eaba7ac26dbf9dcf27b1208ecf4",
    "gvisor-bin/checkpointgofer": "0ec5616635dc17880cd50cb3fc4edf591e7877d9f4bef293d1b3b90cdce7fe2c",
    "gvisor-bin/gvisor-proc-umounter": "c58adb3e05f2d8116a28b4588a17bdb07975b00bdc5a617d14ce08037ab5da36",
    "gvisor-bin/gvisor-sentry-prewarmer": "5108438e45f485b9843daa27537921cf65ef71d1cc0878fec62cda5507ada09c",
    "gvisor-bin/gvisor_sentry": "1e39789a667dfd8691853b37cdafcc36b5ad2f0534c6116141a0cb0791a2c41f",
    "gvisor-bin/runsc-fd-parking": "28f5206c05ce4cffb2a7e55090c3ba7e1fbb285aa59b1f6f152939b964e32600",
    "gvisor-bin/runsc-metric-server": "a5588f69a76d0e1cc732ec02a3bcb1705602c5bcc141a40f7cc4bb47dc9ce28d",
    "gvisor-bin/runsc-netgofer": "c9e8a9d7ba382d3ad36898c8f7dff18344ff5ee2430de16d620d9ceca34c6b46",
    "runsc": "86abcf70c194aad822e21e96a1e1ed61d8082924c794be6205499d7733a8322c"
}
MEMBERS = {"runsc", "containerd-shim-runsc-v1"} | {"gvisor-bin/" + sidecar for sidecar in (
    "gvisor_sentry", "checkpointgofer", "gvisor-sentry-prewarmer", "runsc-fd-parking",
    "runsc-metric-server", "runsc-netgofer", "gvisor-proc-umounter")}
TMP = Path(os.environ["RUNNER_TEMP"])
HANDOFF = TMP / "handoff"
AUDIT = TMP / "runtime-cross/verdict.json"
BZ = ["/usr/local/bin/bazel", "--nosystem_rc", "--nohome_rc"]
ARCH = os.environ["RELEASE_ARCH"]
CONFIG, MACHINE = {"arm64": ("aarch64", 183), "amd64": ("x86_64", 62)}[ARCH]
FLAGS = ["--config=" + CONFIG, "--jobs=4", "--remote_cache=", "--remote_executor=", "--disk_cache="]
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
    assert all(v is None for v in result["system_tools"].values()) and all(v is None for v in result["system_compiler_packages"].values())
    assert all(v is None for v in result["foreign_tools"].values()) and all(v is None for v in result["foreign_packages"].values())
    assert result["target"] == ARCH
    arm = result["targetResult"]
    assert arm["flags"] == FLAGS and arm["build_exit"] == 0 and arm["declared_llvm_consumer_actions_verified"]
    targets = {"//runsc:runsc": "runsc", "//runsc/cmd/sentry:gvisor_sentry": "gvisor-bin/gvisor_sentry"}
    direct = {targets[b["owner"]]: b["sha256"] for b in arm["binaries"]
              if b["machine"] == MACHINE and not b["interpreter"] and not b["dynamic_segment"] and b["fresh_link"]}
    assert direct.keys() == BEFORE.keys()
    RECEIPT.update(phase="build //:release", nativeHost="x86_64", target=CONFIG, builder=result["image_id"],
                   producerAuditSHA256=digest(AUDIT), direct=direct, matchesPrior={p: h == BEFORE[p] for p, h in direct.items()} if ARCH == "arm64" else {})
    save()
    with (TMP / "release-build.log").open("wb") as log:
        cmd = [*BZ, "build", *FLAGS, "--noshow_progress", "--color=no", "--curses=no", "//:release"]
        status = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT).returncode
    RECEIPT["buildExit"] = status
    if status:
        import native_cross_inputs
        RECEIPT["buildDiagnostics"] = native_cross_inputs.diagnostics((TMP / "release-build.log").read_bytes())
    save()
    assert status == 0
    execroot = Path(output(*BZ, "info", "execution_root"))
    outputs = output(*BZ, "cquery", *FLAGS, "--output=files", "//:release").splitlines()
    paths = {}
    for path in outputs:
        prefix, sep, member = path.partition("/bin/release/")
        assert sep and prefix.startswith("bazel-out/") and member in MEMBERS and member not in paths
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
                assert header[:6] == b"\x7fELF\x02\x01" and int.from_bytes(header[18:20], "little") == MACHINE
                data.seek(0)
                tar.addfile(info, data)
            files[name] = {"sha256": digest(path), "bytes": info.size}
        assert all(files[n]["sha256"] == h for n, h in direct.items())
    assert archive.stat().st_size < 200 << 20 and sum(f["bytes"] for f in files.values()) < 400 << 20
    manifest = {"source": SOURCE, "compiler": COMPILER, "nativeHost": "x86_64", "target": CONFIG,
                "builder": result["image_id"], "direct": direct, "files": files, "matchesPrior": RECEIPT["matchesPrior"],
                "archiveSHA256": digest(archive), "archiveBytes": archive.stat().st_size,
                "producerAuditSHA256": digest(AUDIT)}
    meta = HANDOFF / "manifest.json"
    meta.write_text(json.dumps(manifest, sort_keys=True) + "\n")
    if ARCH == "arm64":
        RECEIPT.update(phase="compare prior release", files=files,
                       memberMatchesPrior={name: data["sha256"] == PRIOR_RELEASE[name] for name, data in files.items()})
        save()
        assert all(RECEIPT["memberMatchesPrior"].values()), "ARM64 release bytes changed"
    RECEIPT.update(phase="complete", files=files, manifestSHA256=digest(meta), archiveSHA256=manifest["archiveSHA256"],
                   archiveBytes=manifest["archiveBytes"], verified=True)
    save()


try:
    pack()
except (AssertionError, KeyError, ValueError, OSError, subprocess.SubprocessError) as exc:
    RECEIPT["errorType"] = type(exc).__name__
    save()
    raise
