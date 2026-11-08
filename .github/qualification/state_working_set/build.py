"""Build the unchanged release and guest, then the independent reporting tool."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess

from measure import BUNDLE, INSTALL, OUT, SOURCE, WORK, digest


GUEST = "//test/rbe/restore_advice:guest"
BENCHSTAT = "@org_golang_x_perf//cmd/benchstat:benchstat"
FLAGS = ["--config=x86_64", "-c", "opt", "--jobs=4", "--lockfile_mode=off",
         "--distdir=" + str(WORK / "dist"),
         "--remote_cache=", "--remote_executor=", "--bes_backend=", "--spawn_strategy=linux-sandbox",
         "--strategy=GoStdlib=processwrapper-sandbox", "--strategy=GoStdlibList=processwrapper-sandbox",
         "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox"]
BAZEL = [str(WORK / "bazel"), "--nosystem_rc", "--nohome_rc", "--host_jvm_args=-Xmx4g"]


def run(argv, log):
    with log.open("wb") as dst:
        rc = subprocess.run(argv, stdout=dst, stderr=subprocess.STDOUT).returncode
    if rc:
        text = log.read_text(errors="replace")
        classes = [s for s in ("no such target", "no such package", "missing strict dependencies",
                   "No space left", "no registered toolchains", "Error downloading", "out of memory",
                   "GoCompilePkg", "error loading package") if s.lower() in text.lower()]
        raise RuntimeError((log.name, rc, classes, digest(log)))


def exe(target):
    files = subprocess.check_output([*BAZEL, "cquery", *FLAGS, "--output=files", target], text=True)
    candidates = [Path(n) for n in files.splitlines() if Path(n).is_file() and os.access(n, os.X_OK)]
    assert len(candidates) == 1, target
    return candidates[0]


def build():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    subprocess.run(["git", "diff", "--quiet", "HEAD"], check=True)
    original_guest = Path("test/rbe/restore_advice/guest.go")
    assert subprocess.check_output(["git", "hash-object", original_guest], text=True).strip() == "fa8722188274a32ffecd31a55da785c613585fc8"
    doc = {"source": SOURCE, "build_verified": False}
    out = OUT / "build.json"
    try:
        run([*BAZEL, "build", *FLAGS, "//:release", GUEST], WORK / "runtime-build.log")
        shutil.copytree("bazel-bin/release", INSTALL)
        root = BUNDLE / "rootfs"
        root.mkdir(parents=True)
        shutil.copy2(exe(GUEST), root / "probe")
        names = {"runsc", "containerd-shim-runsc-v1", *("gvisor-bin/" + n for n in
                 ("checkpointgofer","gvisor-proc-umounter","gvisor-sentry-prewarmer","gvisor_sentry",
                  "runsc-fd-parking","runsc-metric-server","runsc-netgofer"))}
        assert {str(p.relative_to(INSTALL)) for p in INSTALL.rglob("*") if p.is_file()} == names
        doc["release"] = {n: digest(INSTALL / n) for n in sorted(names)}
        for path in [root / "probe", *(INSTALL / n for n in names)]:
            with path.open("rb") as f:
                h = f.read(20)
            assert h[:7] == b"\x7fELF\x02\x01\x01" and struct.unpack_from("<H", h, 18)[0] == 62
        doc["guest"] = digest(root / "probe")
        version = subprocess.check_output([INSTALL / "runsc", "--version"], text=True)
        stamp = subprocess.check_output(["git", "describe", "--always", "--tags", "--exclude",
                                         "release-*-staging", "--abbrev=12", "--dirty"], text=True).strip()
        assert stamp != "0.0.0" and stamp in version
        doc["version"] = version.splitlines()[0]
        spec = dict(ociVersion="1.0.2", root=dict(path="rootfs"), process=dict(terminal=False,
                    user=dict(uid=0,gid=0), cwd="/", args=["/probe"], env=["PATH=/","GOMAXPROCS=2"]),
                    mounts=[dict(type="proc",source="proc",destination="/proc")],
                    linux=dict(namespaces=[dict(type=t) for t in ("pid","mount","ipc","uts","network")]))
        (BUNDLE / "config.json").write_text(json.dumps(spec))
        (WORK / "runtime").mkdir()
        out.write_text(json.dumps(doc) + "\n")
        module = Path("MODULE.bazel")
        original = module.read_bytes()
        try:
            additions = (WORK / "benchstat.module").read_bytes()
            module.write_bytes(original + b"\n" + additions)
            run([*BAZEL, "build", *FLAGS, BENCHSTAT], WORK / "tool-build.log")
            shutil.copy2(exe(BENCHSTAT), WORK / "benchstat")
        finally:
            module.write_bytes(original)
        doc["benchstat_sha256"] = digest(WORK / "benchstat")
        doc["benchstat_module_sha256"] = hashlib.sha256(additions).hexdigest()
        run([*BAZEL, "shutdown"], WORK / "shutdown.log")
        doc["build_verified"] = True
    except Exception as exc:
        doc["failure"] = type(exc).__name__ + " " + str(exc)[:450]
        raise
    finally:
        out.write_text(json.dumps(doc, indent=2) + "\n")


if __name__ == "__main__":
    build()
