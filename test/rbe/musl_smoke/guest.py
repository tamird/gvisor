#!/usr/bin/python3
"""Run the packaged Sentry in the private native user namespace from 71c2ea47c."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys


SOURCE = "3dab3494e2a2"
MARKER = "gvisor-systrap-counted-64-lines"
RACE_BINARIES = {"runsc", "gvisor-bin/gvisor_sentry"}
BUSYBOX = "52151e7f322f926b64049cdaa1410dc3ea6485525e0624b05813791c219ae933"
ROOT = Path(os.environ["TEST_TMPDIR"]).resolve()
REPORT = {}


def save():
    data = json.dumps(REPORT, sort_keys=True)
    Path(os.environ["TEST_UNDECLARED_OUTPUTS_DIR"], "receipt.json").write_text(data)
    print("GUEST_RECEIPT " + data, flush=True)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def diagnostics(output):
    return [line[:600] for line in output.decode(errors="replace").splitlines()
            if re.search(r"error|fail|fatal|panic|signal|segmentation|tsan|unexpected|denied|not permitted|cannot|timed out|not found|unshare:|mount:", line, re.I)][-20:]


def launch(directory, install, busybox):
    # The caller is an ordinary user. The process becomes root only in its
    # new user namespace and replaces the host filesystem with this fixture.
    (directory / "bundle/rootfs").mkdir(parents=True)
    for name in ("proc", "sys", "dev", "tmp", "run"):
        (directory / name).mkdir()
    (directory / "busybox").hardlink_to(busybox)
    (directory / "bundle/rootfs/busybox").hardlink_to(busybox)
    if install:
        shutil.copytree(install, directory / "release", copy_function=os.link)
    command = 'test "$(/busybox uname -m)" = aarch64; test "$(/busybox seq 1 64 | /busybox wc -l)" -eq 64; echo ' + MARKER
    bundle = {"ociVersion": "1.0.2", "root": {"path": "rootfs", "readonly": True},
              "process": {"args": ["/busybox", "sh", "-ec", command], "cwd": "/", "user": {"uid": 0, "gid": 0},
                          "env": ["PATH=/"], "noNewPrivileges": True},
              "linux": {"namespaces": [{"type": n} for n in ("pid", "network", "ipc", "uts", "mount")]}}
    (directory / "bundle/config.json").write_text(json.dumps(bundle))
    setup = r'''set -eu
root=$1
test "$(id -u)" = 0
test "$(awk 'NR==1{print $2,$3}' /proc/self/uid_map)" = "$2 1"
test "$(readlink /proc/self/ns/user)" != "$3"
test "$(readlink /proc/self/ns/net)" != "$4"
test "$(readlink /proc/self/ns/pid)" != "$5"
mount --bind "$root" "$root"
mount -t proc proc "$root/proc"
mount -t sysfs -o ro sysfs "$root/sys"
for d in null zero full random urandom; do
    touch "$root/dev/$d"
    mount --bind "/dev/$d" "$root/dev/$d"
done
ln -s /proc/self/fd "$root/dev/fd"
ln -s /proc/self/fd/2 "$root/dev/stderr"
cd "$root"
/usr/sbin/pivot_root . .
exec /busybox sh -ec '
  /busybox umount -l .
  cd /
  test "$(/busybox id -u)" = 0
  test ! -e /var/run/docker.sock
  echo NATIVE_GUEST_PRIVATE_ROOT >&2
  COMMAND
'
'''
    inner = ("exec /release/runsc --rootless --root=/run/state --platform=systrap --network=none "
             "--ignore-cgroups=true --sidecar-release-enforcement-policy=ALWAYS "
             "--debug-log=/dev/stderr run --bundle=/bundle musl-smoke") if install else "exec /busybox true"
    names = [os.readlink("/proc/self/ns/" + kind) for kind in ("user", "net", "pid")]
    args = ["/usr/bin/setpriv", "--no-new-privs", "/usr/bin/unshare", "--user", "--map-root-user",
            "--mount", "--net", "--pid", "--fork", "--kill-child", "--ipc", "--uts", "--propagation", "private",
            "/bin/sh", "-ec", setup.replace("COMMAND", inner), "smoke", str(directory), str(os.getuid()), *names]
    out_path, err_path = directory / "stdout", directory / "stderr"
    with out_path.open("wb") as out, err_path.open("wb") as err:
        try:
            status = subprocess.run(args, stdout=out, stderr=err, timeout=100).returncode
        except subprocess.TimeoutExpired:
            status = 124
    stdout, stderr = out_path.read_bytes(), err_path.read_bytes()
    return {"exit": status, "private_root": b"NATIVE_GUEST_PRIVATE_ROOT" in stderr.splitlines(),
            "marker": stdout.splitlines() == [MARKER.encode()], "stdout_sha256": sha(stdout),
            "stderr_sha256": sha(stderr), "diagnostics": diagnostics(stderr),
            "stdio": bool(re.search(rb"Remapped stdio FDs to \[\d+, \d+\] in [^\s]+", stderr)),
            "sentry": b'Sidecar "gvisor_sentry" found: booting sandbox' in stderr,
            "prewarmer": b'Sidecar "gvisor-sentry-prewarmer" found: prepending Sentry boot command' in stderr,
            "release": f'[Sidecar release match check] Build label "{SOURCE}" matches the expected release in sidecar "gvisor_sentry"'.encode() in stderr}


def main():
    release, fixture = [Path(arg).resolve(strict=True) for arg in sys.argv[1:3]]
    mode = sys.argv[3]
    REPORT.update(mode=mode, source=SOURCE, uid=os.getuid(), binaries={})
    assert os.getuid() != 0 and os.uname().machine == "aarch64" and mode in ("normal", "race")
    busybox = ROOT / "busybox"
    shutil.copy2(fixture, busybox)
    assert sha(busybox.read_bytes()) == BUSYBOX
    busybox.chmod(0o755)
    install = ROOT / "install"
    shutil.copytree(release, install)
    for dest in sorted(path for path in install.rglob("*") if path.is_file()):
        name = str(dest.relative_to(install))
        public = release / name
        raw = dest.read_bytes()
        assert raw[:6] == b"\x7fELF\x02\x01" and int.from_bytes(raw[18:20], "little") == 183
        offset = struct.unpack_from("<Q", raw, 32)[0]
        stride, count = struct.unpack_from("<HH", raw, 54)
        types = [struct.unpack_from("<I", raw, offset + i * stride)[0] for i in range(count)]
        row = REPORT["binaries"][name] = {
            "release_name": public.name, "release_sha256": sha(public.read_bytes()), "staged_sha256": sha(raw),
            "bytes": len(raw), "static": 2 not in types and 3 not in types,
            "raceinit": b"runtime.raceinit" in raw, "tsan": b"__tsan_" in raw}
        assert row["release_sha256"] == row["staged_sha256"]
        if name in RACE_BINARIES:
            assert row["raceinit"] == row["tsan"] == (mode == "race")
            if mode == "race":
                assert row["static"]
    assert len(REPORT["binaries"]) == 9 and "gvisor-bin/gvisor-proc-umounter" in REPORT["binaries"]
    version = subprocess.run([str(install / "runsc"), "--version"], capture_output=True, timeout=15)
    REPORT["cli_version"] = {"exit": version.returncode, "stdout": version.stdout.decode()[:200],
                             "diagnostics": diagnostics(version.stderr)}
    assert version.returncode == 0 and SOURCE.encode() in version.stdout
    save()
    REPORT["namespace_control"] = launch(ROOT / "control", None, busybox)
    save()
    assert REPORT["namespace_control"]["exit"] == 0 and REPORT["namespace_control"]["private_root"]
    guest = REPORT["guest"] = launch(ROOT / "guest", install, busybox)
    assert guest["exit"] == 0 and all(guest[k] for k in ("private_root", "marker", "stdio", "sentry", "prewarmer", "release"))


if __name__ == "__main__":
    try:
        main()
    finally:
        save()
