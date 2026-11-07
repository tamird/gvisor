#!/usr/bin/env python3
"""Run the real cmd_test target and its declared guest on the native hosted kernel."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

OWNER = "//runsc/cmd:cmd_test"
NAMESPACES = ("mnt", "pid", "net", "uts", "ipc")
SIDECARS = {"checkpointgofer", "gvisor-proc-umounter", "gvisor-sentry-prewarmer", "gvisor_sentry",
            "runsc-fd-parking", "runsc-metric-server", "runsc-netgofer"}
MACHINES = {"aarch64": 183, "x86_64": 62}
def digest(p):
    h = hashlib.sha256()
    with p.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def elf(p, static=False):
    with p.open("rb") as f:
        h = f.read(64)
        assert h[:6] == b"\x7fELF\x02\x01"
        machine = int.from_bytes(h[18:20], "little")
        assert machine == MACHINES[platform.machine()]
        f.seek(int.from_bytes(h[32:40], "little"))
        size = int.from_bytes(h[54:56], "little")
        count = int.from_bytes(h[56:58], "little")
        assert 56 <= size <= 128 and 0 < count < 200
        headers = [f.read(size) for _ in range(count)]
        assert all(len(header) == size for header in headers)
        kinds = [int.from_bytes(header[:4], "little") for header in headers]
        assert 1 in kinds
        needed = 0
        for header, kind in zip(headers, kinds):
            if kind == 2:
                f.seek(int.from_bytes(header[8:16], "little"))
                length = int.from_bytes(header[32:40], "little")
                assert length % 16 == 0 and length < 1024 * 1024
                for _ in range(length // 16):
                    tag = int.from_bytes(f.read(16)[:8], "little")
                    if tag == 0:
                        break
                    needed += tag == 1
        if static:
            assert 3 not in kinds and not needed, "guest loader or dynamic dependencies"
    return {"machine": machine, "size": p.stat().st_size, "sha256": digest(p),
            "interpreter": 3 in kinds, "needed": needed}


def ns(k):
    return os.readlink("/proc/self/ns/" + k)


def run_under(args):
    # Bazel sets this from the go_test's $(rootpath); never inject a guessed value.
    app = os.environ["GVISOR_CMD_TEST_APP"]
    root = Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"]
    assert not Path(app).is_absolute() and ".." not in Path(app).parts and app.startswith("test/cmd/test_app/")
    assert os.environ["TEST_TARGET"] == OWNER and os.getuid() != 0
    source = root / app
    assert source.is_file() and os.access(source, os.X_OK)
    actual = elf(source, static=True)
    allow = {"HOME", "LANG", "PATH", "CGROUPV2", "GVISOR_CMD_TEST_APP", "XML_OUTPUT_FILE", "TESTBRIDGE_TEST_ONLY",
             "JAVA_RUNFILES", "PYTHON_RUNFILES"}
    env = [f"{k}={v}" for k, v in os.environ.items()
           if k in allow or k.startswith(("TEST_", "RUNFILES_", "GO_TEST_", "GTEST_"))]
    env.extend(f"PARENT_NS_{k}={ns(k)}" for k in NAMESPACES)
    with tempfile.TemporaryDirectory(prefix="hosted-cmd-relay-", dir="/tmp") as tmp:
        relay = Path(tmp)
        for name in ("xml", "facts.json"):
            (relay / name).touch()
        code = subprocess.run(["/usr/bin/sudo", "-n", "--", "/usr/bin/unshare", "--fork",
                               "--mount", "--propagation", "private", "--mount-proc", "--pid",
                               "--net", "--uts", "--ipc", "--", "/usr/bin/env", "-i", *env,
                               "/usr/bin/python3", "-IB", str(Path(__file__).resolve()), "inside",
                               str(relay), *args]).returncode
        if (relay / "xml").stat().st_size:
            shutil.copyfile(relay / "xml", os.environ["XML_OUTPUT_FILE"])
        inner = json.loads((relay / "facts.json").read_text())
        print("CMD_GUEST_ORIGINAL " + json.dumps({"rootpath": app, "test_app": actual, **inner},
                                                  sort_keys=True), flush=True)
        return code


def inside(relay, args):
    assert os.getuid() == 0 and all(ns(k) != os.environ.pop("PARENT_NS_" + k) for k in NAMESPACES)
    src = Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"] / "release/gvisor-bin"
    assert {p.name for p in src.iterdir()} == SIDECARS
    with (tempfile.TemporaryDirectory(prefix="hosted-cmd-sidecars-", dir="/tmp") as raw,
          tempfile.TemporaryDirectory(prefix="hosted-cmd-inner-", dir="/tmp") as private,
          tempfile.TemporaryDirectory(prefix="hosted-cmd-chroot-", dir="/tmp") as mountpoint):
        target = Path(raw)
        target.chmod(0o755)
        for name in SIDECARS:
            f = target / name
            shutil.copyfile(src / name, f)
            f.chmod(0o755)
            assert digest(f) == digest(src / name)
        # Stage the independently built stock native release in the same
        # public-readable sidecar hierarchy as the original corrected runtime.
        original = Path(__file__).resolve().parent / "old-release"
        old_release = target / "stock"
        old_sidecars = old_release / "gvisor-bin"
        old_sidecars.mkdir(parents=True)
        for name in ["runsc", *("gvisor-bin/" + n for n in sorted(SIDECARS))]:
            source = original / name
            dest = old_release / name
            shutil.copyfile(source, dest)
            dest.chmod(0o755)
            assert digest(source) == digest(dest)
        os.environ["GVISOR_CKPT_OLD_RELEASE"] = str(old_release)
        Path(mountpoint).chmod(0o711)
        work = Path(private)
        (work / "tmp").mkdir()
        xml = work / "test.xml"
        os.environ.update(GVISOR_SIDECAR_BINARIES_DIR=raw, TMPDIR=mountpoint,
                          TEST_TMPDIR=str(work / "tmp"), XML_OUTPUT_FILE=str(xml))
        for k in ("TEST_UNDECLARED_OUTPUTS_DIR", "TEST_UNDECLARED_OUTPUTS_ANNOTATIONS_DIR"):
            (work / k).mkdir()
            os.environ[k] = str(work / k)
        result = subprocess.run(args).returncode
        if xml.is_file():
            shutil.copyfile(xml, Path(relay) / "xml")
        (Path(relay) / "facts.json").write_text(json.dumps(
            {"kernel": platform.release(), "uname": platform.machine(), "sidecars": len(SIDECARS),
             "current_sidecars": {name: elf(target / name) for name in sorted(SIDECARS)},
             "private_namespaces": list(NAMESPACES), "inner_exit": result}))
        return result



if __name__ == "__main__":
    if sys.argv[1] == "inside":
        sys.exit(inside(sys.argv[2], sys.argv[3:]))
    assert sys.argv[1] == "under"
    sys.exit(run_under(sys.argv[2:]))
