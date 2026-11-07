"""Boot immutable, published ARM64 kernels with one initramfs and GTest."""

import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys

CASES = {
    "current": (
        "5.10.0",
        "957d6da5c7ee7869c8a285784a201ac3928393087e2542131d4d107fc9556b45",
    ),
    "draft": (
        "6.1.0",
        "15e7206a252b19807dcba81f42c61b42923ca706ce85a5882248162cf1317d70",
    ),
}
GTEST = "SeccompTest.TsyncAppliesToAllThreads"
WORK = Path(os.environ["TEST_TMPDIR"])
OUT = Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"]) / "kernel-pair.json"
# vda is the guest's own blank result disk. No network or host filesystem
# is attached; its root and test executable are identical in both arms.
INIT = b"""#!/bin/busybox sh
BB=/bin/busybox
$BB mount -t devtmpfs devtmpfs /dev || exit 1
$BB mount -t proc proc /proc || exit 1
i=0
while [ ! -b /dev/vda ] && [ "$i" -lt 60 ]; do $BB sleep 0.1; i=$((i+1)); done
if [ -c /dev/hvc0 ]; then exec >/dev/hvc0 2>&1; fi
export PATH=/bin LD_LIBRARY_PATH=/lib/test LANG=C GTEST_COLOR=no
echo TSYNC_INIT_ENTER
/test --gtest_filter=SeccompTest.TsyncAppliesToAllThreads > /tmp/gtest 2>&1
rc=$?
{
  echo TSYNC_PAIR_V1
  echo "release=$($BB uname -r)"
  echo "machine=$($BB uname -m)"
  echo "test_sha=$($BB sha256sum /test | $BB cut -d' ' -f1)"
  echo "exit=$rc"
  $BB grep '^TSYNC_CONTROL ' /tmp/gtest
  if $BB grep -Fq '[       OK ] SeccompTest.TsyncAppliesToAllThreads' /tmp/gtest; then echo gtest_pass=1; else echo gtest_pass=0; fi
  echo TSYNC_PAIR_DONE
} > /tmp/result
$BB dd if=/tmp/result of=/dev/vda bs=512 conv=sync 2>/dev/null || exit 1
$BB sync
echo TSYNC_RESULT_WRITTEN
$BB reboot -f
"""


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save(value):
    OUT.write_text(json.dumps(value, indent=2) + "\n")


def initramfs(binary):
    probe = binary.read_bytes()
    assert (
        probe[:6] == b"\x7fELF\x02\x01"
        and int.from_bytes(probe[18:20], "little") == 183
    )
    busybox = Path("/bin/busybox").read_bytes()
    assert (
        busybox[:6] == b"\x7fELF\x02\x01"
        and int.from_bytes(busybox[18:20], "little") == 183
    )
    executable = stat.S_IFREG | 0o755
    entries = {
        "init": (executable, INIT),
        "bin/busybox": (executable, busybox),
        "test": (executable, probe),
    }
    libraries = {}
    # Stage the linked ARM64 test's dependencies. The kernel opens the ELF
    # interpreter at the embedded absolute path before LD_LIBRARY_PATH applies.
    elf = subprocess.check_output(["/usr/bin/readelf", "-lW", binary], text=True)
    loader = re.search(r"Requesting program interpreter: (/[^\]]+)\]", elf)
    if loader:
        interp = Path(loader[1])
        entries[str(interp).lstrip("/")] = (executable, interp.read_bytes())
        report = subprocess.check_output(["/usr/bin/ldd", binary], text=True)
        assert "not found" not in report
        for name, actual in re.findall(
            r"(?m)^\s*(\S+) => (/[^(]+?)\s+\(0x[0-9a-f]+\)$", report
        ):
            assert re.fullmatch(r"[A-Za-z0-9+_.-]+", name)
            blob = Path(actual).read_bytes()
            entries["lib/test/" + name] = (executable, blob)
            libraries[name] = sha(blob)
    bbelf = subprocess.check_output(
        ["/usr/bin/readelf", "-lW", "/bin/busybox"], text=True
    )
    assert "Requesting program interpreter" not in bbelf
    for directory in ("bin", "dev", "proc", "tmp", "lib", "lib/test"):
        entries[directory] = (stat.S_IFDIR | 0o755, b"")
    for name in list(entries):
        for parent in Path(name).parents:
            if str(parent) != ".":
                entries.setdefault(str(parent), (stat.S_IFDIR | 0o755, b""))
    entries.update(
        {
            "dev/console": (stat.S_IFCHR | 0o600, b""),
            "dev/null": (stat.S_IFCHR | 0o666, b""),
        }
    )
    archive = io.BytesIO()
    for index, name in enumerate([*sorted(entries), "TRAILER!!!"], 1):
        mode, data = entries.get(name, (0, b""))
        major, minor = {"dev/console": (5, 1), "dev/null": (1, 3)}.get(name, (0, 0))
        fields = [
            index,
            mode,
            0,
            0,
            2 if stat.S_ISDIR(mode) else 1,
            0,
            len(data),
            0,
            0,
            major,
            minor,
            len(name.encode()) + 1,
            0,
        ]
        archive.write(
            b"070701"
            + "".join(f"{v:08x}" for v in fields).encode()
            + name.encode()
            + b"\0"
        )
        archive.write(b"\0" * (-archive.tell() % 4))
        archive.write(data)
        archive.write(b"\0" * (-archive.tell() % 4))
    archive.write(b"\0" * (-archive.tell() % 512))
    packed = gzip.compress(archive.getvalue(), compresslevel=1, mtime=0)
    initrd = WORK / "root.cpio.gz"
    initrd.write_bytes(packed)
    return initrd, {
        "initramfs_sha256": sha(packed),
        "test_sha256": sha(probe),
        "busybox_sha256": sha(busybox),
        "libraries": libraries,
    }


def boot(which, kernel, initrd, identity, row):
    release, expected = CASES[which]
    image = kernel.read_bytes()
    assert sha(image) == expected and image[56:60] == b"ARM\x64"
    row.update(kernel_sha256=expected, kernel_bytes=len(image))
    disk = WORK / (which + ".raw")
    disk.write_bytes(bytes(1 << 20))
    console = WORK / (which + ".console")
    monitor = WORK / (which + ".qemu")
    cmd = [
        "/usr/bin/qemu-system-aarch64",
        "-nodefaults",
        "-machine",
        "virt,gic-version=3",
        "-accel",
        "tcg,thread=multi",
        "-cpu",
        "cortex-a72",
        "-smp",
        "2",
        "-m",
        "768",
        "-display",
        "none",
        "-monitor",
        "none",
        "-serial",
        "none",
        "-nic",
        "none",
        "-no-reboot",
        "-kernel",
        str(kernel),
        "-initrd",
        str(initrd),
        "-append",
        "console=hvc0 rdinit=/init panic=1",
        "-drive",
        f"if=none,id=result,format=raw,file={disk}",
        "-device",
        "virtio-blk-device,drive=result",
        "-chardev",
        f"file,id=output,path={console}",
        "-device",
        "virtio-serial-device",
        "-device",
        "virtconsole,chardev=output",
    ]
    with monitor.open("wb") as log:
        try:
            row["qemu_exit"] = subprocess.run(
                cmd, stdout=log, stderr=log, timeout=110
            ).returncode
        except subprocess.TimeoutExpired:
            row["qemu_exit"] = 124
    serial = console.read_bytes() if console.exists() else b""
    diag = monitor.read_bytes() + serial
    row["hints"] = [
        tag
        for tag, needle in {
            "kernel-panic": b"Kernel panic",
            "init-entered": b"TSYNC_INIT_ENTER",
            "result-written": b"TSYNC_RESULT_WRITTEN",
            "no-init": b"No working init",
            "missing-emulator-option": b"not a valid device",
            "unavailable-device": b"No '",
            "loader": b"Failed to execute /init",
        }.items()
        if needle in diag
    ]
    raw = disk.read_bytes().split(b"\0", 1)[0]
    assert 0 < len(raw) <= 4096
    lines = raw.decode("ascii").splitlines()
    assert lines[0] == "TSYNC_PAIR_V1" and lines[-1] == "TSYNC_PAIR_DONE"
    controls = [line for line in lines if line.startswith("TSYNC_CONTROL ")]
    base = dict(
        line.split("=", 1)
        for line in lines[1:-1]
        if not line.startswith("TSYNC_CONTROL ")
    )
    assert set(base) == {"release", "machine", "test_sha", "exit", "gtest_pass"}
    assert re.fullmatch(r"[A-Za-z0-9_.+-]{1,65}", base["release"])
    row.update(
        release=base["release"],
        aarch64=base["machine"] == "aarch64",
        test_sha_matches=base["test_sha"] == identity,
        test_exit=int(base["exit"]),
        gtest_pass=base["gtest_pass"] == "1",
        marker_count=len(controls),
    )
    if len(controls) != 1:
        return
    matched = re.fullmatch(
        r"TSYNC_CONTROL peer_delta=(-?[0-9]{1,5}) installer_delta=(-?[0-9]{1,5}) same_before=([01]) counts=([01]) peer_baseline=(-?1|0) installer_baseline=(-?1|0) peer_enforced=(-?1|0) installer_enforced=(-?1|0)",
        controls[0],
    )
    assert matched
    keys = (
        "peer_delta",
        "installer_delta",
        "same_before",
        "counts",
        "peer_baseline",
        "installer_baseline",
        "peer_enforced",
        "installer_enforced",
    )
    row["control"] = dict(zip(keys, map(int, matched.groups())))
    row["measured"] = (
        row["release"] == release
        and row["aarch64"]
        and row["test_sha_matches"]
        and row["gtest_pass"]
        and row["test_exit"] == 0
        and all(row["control"][k] == 1 for k in keys[2:])
    )


def main():
    arch = platform.machine()
    result = {
        "host_arch": arch,
        "hypervisor": "qemu-system-aarch64",
        "acceleration": "TCG",
        "guest_cpus": 2,
        "case": GTEST,
        "shared_worker": False,
        "kernels": {},
        "comparison_verified": False,
    }
    save(result)
    try:
        assert arch == "aarch64"
        current, draft, binary = [Path(s).resolve(strict=True) for s in sys.argv[1:]]
        version = subprocess.check_output(
            ["/usr/bin/qemu-system-aarch64", "--version"], text=True
        )
        result["qemu_version"] = re.search(r"QEMU emulator version ([0-9.]+)", version)[
            1
        ]
        initrd, identity = initramfs(binary)
        result.update(identity)
        for name, kernel in (("current", current), ("draft", draft)):
            row = result["kernels"][name] = {"measured": False}
            try:
                boot(name, kernel, initrd, identity["test_sha256"], row)
            except (
                OSError,
                ValueError,
                IndexError,
                AssertionError,
                subprocess.SubprocessError,
            ) as e:
                row["error"] = type(e).__name__
            save(result)
        rows = result["kernels"]
        result["comparison_verified"] = (
            all(r["measured"] for r in rows.values())
            and rows["current"]["control"]["peer_delta"] == 0
            and rows["draft"]["control"]["peer_delta"] == 1
            and all(r["control"]["installer_delta"] == 1 for r in rows.values())
        )
    except (
        OSError,
        ValueError,
        AssertionError,
        IndexError,
        subprocess.SubprocessError,
    ) as e:
        result["preparation_error"] = type(e).__name__
    finally:
        save(result)
    return result["comparison_verified"]


if __name__ == "__main__":
    raise SystemExit(not main())
