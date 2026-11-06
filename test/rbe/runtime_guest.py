"""Boot the source-default LLVM runtime in the proven private native root."""
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import sys
import tarfile

import runtime_cross as r

q = r.q
PREWARMER = "//runsc/prewarmer:prewarmer_binary"
r.ROOTS = [*r.ROOTS, PREWARMER]
q.ROOTS = r.ROOTS
q.owned = lambda label: r.owned(label) or label == PREWARMER
EXPECTED = {
    "//runsc:runsc": ("runsc", "a5f9011b7f0be6c1bc4be165e011cd1137c2344195055059ab289ca75e509773"),
    "//runsc/cmd/sentry:gvisor_sentry": ("gvisor-bin/gvisor_sentry", "30839d7ea34ee324547d1bdd61ac992ebe871cfb81d3d6bac0075697ae5060d4"),
}
PACKAGE = "pool/main/b/busybox/busybox-static_1.36.1-6ubuntu3.1_amd64.deb"
PACKAGE_SHA = "944b2728f53ceb3916cec2c962873c9951e612408099601751db2a0a5d81e0ed"
BUSYBOX_SHA = "dbac288c29ba568459550a2da9e7ae0ded6b1fc728ee9fad3044c44e62d6ac14"
MEMBERS = {"runsc", "gvisor-bin/gvisor_sentry", "gvisor-bin/gvisor-sentry-prewarmer"}


def install_file(source, destination, expected):
    shutil.copy2(source, destination)
    raw = destination.read_bytes()
    assert len(raw) == expected["bytes"] and q.digest(raw) == expected["sha256"]


def fixture(row, arch="amd64"):
    package_path, package_sha, payload_sha, payload_bytes, machine, mirror = {
        "amd64": (PACKAGE, PACKAGE_SHA, BUSYBOX_SHA, 2124608, 62, "https://archive.ubuntu.com/ubuntu/"),
        "arm64": ("pool/main/b/busybox/busybox-static_1.36.1-6ubuntu3.1_arm64.deb", "d96535e0402c011e0ee43449799df2f4504d44b842e4f2b3a6cbc845508eaafc", "52151e7f322f926b64049cdaa1410dc3ea6485525e0624b05813791c219ae933", 1914704, 183, "https://ports.ubuntu.com/ubuntu-ports/"),
    }[arch]
    package = r.TASK / "busybox.deb"
    row["downloads"] = []
    for base in ("https://snapshot.ubuntu.com/ubuntu/20260928T000000Z/", mirror):
        code, _, err = q.command(["curl", "--fail", "--silent", "--show-error", "--location", "--proto", "=https", "--proto-redir", "=https", "--connect-timeout", "10", "--max-time", "25", "--retry", "2", base + package_path, "-o", str(package)], 100)
        row["downloads"].append({"exit": code, "diagnostics": q.diagnostics(err)})
        if code == 0:
            break
    assert code == 0 and 0 < package.stat().st_size < 4000000
    row["package_sha256"] = q.digest(package.read_bytes())
    assert row["package_sha256"] == package_sha
    code, data, err = q.command(["ar", "p", str(package), "data.tar.zst"])
    assert code == 0 and 0 < len(data) < 4000000, q.diagnostics(err)
    compressed = r.TASK / "data.tar.zst"
    compressed.write_bytes(data)
    code, data, err = q.command(["zstd", "-d", "-c", str(compressed)])
    assert code == 0 and 0 < len(data) < 16000000, q.diagnostics(err)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        member, = [m for m in archive.getmembers() if m.name.removeprefix("./") == "usr/bin/busybox"]
        assert member.isfile() and member.size == payload_bytes
        raw = archive.extractfile(member).read()
    meta, _, _ = q.elf(raw)
    row["elf"] = meta
    assert meta["sha256"] == payload_sha and meta["machine"] == machine and not meta["interpreter"]
    path = r.TASK / "busybox"
    path.write_bytes(raw)
    path.chmod(0o755)
    return path


def capsule(name, busybox, install=None):
    root = r.TASK / name
    (root / "bundle/rootfs").mkdir(parents=True)
    for directory in ("proc", "sys", "dev", "tmp", "run"):
        (root / directory).mkdir()
    (root / "busybox").hardlink_to(busybox)
    (root / "bundle/rootfs/busybox").hardlink_to(busybox)
    if install is not None:
        shutil.copytree(install, root / "release", copy_function=os.link)
        assert all((root / "release" / p).samefile(install / p) for p in MEMBERS)
    marker = "default-llvm-sentry-running-under-systrap"
    (root / "bundle/config.json").write_text(json.dumps({
        "ociVersion": "1.0.2", "root": {"path": "rootfs", "readonly": True},
        "process": {"args": ["/busybox", "echo", marker], "cwd": "/", "user": {"uid": 0, "gid": 0}, "env": ["PATH=/"], "noNewPrivileges": True},
        "linux": {"namespaces": [{"type": n} for n in ("pid", "network", "ipc", "uts", "mount")]},
    }))
    return root, marker


def launch(root, row, control=False):
    # Same user/mount/network/PID capsule as c911d4a545, before any product runs.
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
pivot_root . .
exec /busybox sh -ec '
  /busybox umount -l .
  cd /
  test "$(/busybox id -u)" = 0
  test ! -e /var/run/docker.sock
  echo NATIVE_GUEST_PRIVATE_ROOT >&2
  COMMAND
'
'''
    command = "exec /busybox true" if control else "exec /release/runsc --rootless --root=/run/state --platform=systrap --network=none --ignore-cgroups=true --sidecar-release-enforcement-policy=ALWAYS --debug-log=/dev/stderr run --bundle=/bundle default-llvm-smoke"
    setup = setup.replace("COMMAND", command)
    names = [os.readlink("/proc/self/ns/" + n) for n in ("user", "net", "pid")]
    args = ["/usr/bin/setpriv", "--no-new-privs", "/usr/bin/unshare", "--user", "--map-root-user", "--mount", "--net", "--pid", "--fork", "--kill-child", "--ipc", "--uts", "--propagation", "private", "/bin/sh", "-ec", setup, "guest", str(root), str(os.getuid()), *names]
    code, stdout, stderr = q.command(args, 120)
    row.update(exit=code, stdout_sha256=q.digest(stdout), stderr=q.diagnostics(stderr), private_root_entered=b"NATIVE_GUEST_PRIVATE_ROOT" in stderr.splitlines())
    row["sentry_stdio_witness"] = bool(re.search(rb"Remapped stdio FDs to \[\d+, \d+\] in [^\s]+", stderr))
    row["prewarmer_selected"] = b'Sidecar "gvisor-sentry-prewarmer" found: prepending Sentry boot command' in stderr
    row["sentry_selected"] = b'Sidecar "gvisor_sentry" found: booting sandbox' in stderr
    row["sentry_release_matches"] = f'[Sidecar release match check] Build label "{r.SOURCE[:12]}" matches the expected release'.encode() in stderr
    row["optional_fdparking_absent"] = b'Sidecar "runsc-fd-parking" not found or usable' in stderr
    return stdout


def prewarmer(runtime, install, row):
    flags = runtime["flags"]
    code, raw, err = q.command([r.BAZEL, "aquery", *flags, "--output=jsonproto", "--include_param_files", 'mnemonic("Genrule", ' + PREWARMER + ')'], 180)
    row.update(aquery_exit=code, diagnostics=q.diagnostics(err))
    assert code == 0
    action, = q.graph(raw)
    row["action"] = action
    assert action["owner"] == PREWARMER and action["mnemonic"] == "Genrule"
    assert "external/llvm++llvm_toolchain_minimal+llvm-toolchain-minimal-linux-amd64/bin/clang" in " ".join(action["argv"])
    code, raw, err = q.command([r.BAZEL, "cquery", *flags, "--output=files", PREWARMER], 120)
    assert code == 0, q.diagnostics(err)
    output, = raw.decode().splitlines()
    assert output in action["outputs"]
    row["completed"] = []
    for line in (r.TASK / "amd64.bep").read_text().splitlines():
        event = json.loads(line)
        key = event.get("id", {}).get("actionCompleted", {})
        if key.get("label") != PREWARMER:
            continue
        value = event["action"]
        row["completed"].append({"owner": key["label"], "output": key.get("primaryOutput"), "configuration": key.get("configuration", {}).get("id"), "type": value.get("type"), "success": value.get("success", False), "exit": value.get("exitCode", 0)})
    completed, = row["completed"]
    assert completed["success"] and completed["exit"] == 0 and completed["type"] == "Genrule"
    assert completed["output"] == output and completed["configuration"] == action["configuration"]
    assert runtime["events"]["configurations"][completed["configuration"]] == "k8"
    code, raw, err = q.command([r.BAZEL, "info", "execution_root"], 60)
    assert code == 0, q.diagnostics(err)
    path, meta = r.physical(Path(raw.decode().strip()), output)
    row["elf"] = meta
    assert meta["machine"] == 62 and not meta["interpreter"] and not meta["dynamic_segment"] and meta["bytes"] <= 4096
    assert struct.unpack_from("<Q", path.read_bytes(), 24)[0]
    destination = install / "gvisor-bin/gvisor-sentry-prewarmer"
    install_file(path, destination, meta)


def main(result, save):
    assert platform.machine() == "x86_64" and os.getuid() != 0
    q.source(r.SOURCE)
    result["phase"] = "fixture"
    busybox = fixture(result.setdefault("fixture", {}))
    root, _ = capsule("namespace-control", busybox)
    row = result["namespace_control"] = {}
    launch(root, row, control=True)
    save()
    assert row["exit"] == 0 and row["private_root_entered"]
    result["phase"] = "native-build"
    runtime = result["runtime"] = {}
    save()
    r.phase("amd64", "x86_64", "k8", 62, runtime, save)
    assert runtime.get("verified")
    code, raw, err = q.command([r.BAZEL, "info", "execution_root"], 60)
    assert code == 0, q.diagnostics(err)
    execroot = Path(raw.decode().strip())
    install = r.TASK / "install"
    (install / "gvisor-bin").mkdir(parents=True)
    for binary in runtime["binaries"]:
        dest, expected = EXPECTED[binary["owner"]]
        assert binary["sha256"] == expected
        install_file(execroot / binary["path"], install / dest, binary)
    result["phase"] = "prewarmer"
    prewarmer(runtime, install, result.setdefault("prewarmer", {}))
    result["installed_files"] = sorted(str(p.relative_to(install)) for p in install.rglob("*") if p.is_file())
    assert set(result["installed_files"]) == MEMBERS
    root, marker = capsule("guest-root", busybox, install)
    result["phase"] = "native-guest"
    save()
    row = result["smoke"] = {"platform": "systrap", "network": "none", "rootless": True, "native_arch": "amd64"}
    stdout = launch(root, row)
    row["marker"] = stdout.splitlines() == [marker.encode()]
    save()
    assert row["exit"] == 0 and all(row[k] for k in ("private_root_entered", "sentry_stdio_witness", "sentry_selected", "prewarmer_selected", "sentry_release_matches", "marker"))
    result["status"] = "PASS"


if __name__ == "__main__":
    result = {"status": "INCOMPLETE", "phase": "preflight", "source": r.SOURCE, "worker": "native-amd64", "scope": "minimal rootless systrap guest; no native ARM runtime claim", "original_runtime": {"run": 42082145768, "artifact": 13443015162, "sha256": "34ac357b9eaf4a9c14fcac8dba3e0d27ba54fa865ee6f38fa9d7e65f772972bd"}, "namespace_donor": "c911d4a545", "original_native_guest_artifact": 13391486689}
    def save():
        raw = json.dumps(result, indent=2).encode()
        assert len(raw) < 524288
        (r.TASK / "verdict.json").write_bytes(raw)
    save()
    try:
        main(result, save)
    except (AssertionError, OSError, KeyError, ValueError, struct.error, tarfile.TarError) as error:
        result["error"] = type(error).__name__ + ": " + str(error)[:500]
        build_exit = result.get("runtime", {}).get("build_exit")
        smoke = result.get("smoke", {})
        if build_exit not in (None, 0, 124, 137) or (smoke.get("private_root_entered") and smoke.get("exit") not in (None, 0, 124, 137)):
            result["status"] = "FAIL"
    save()
    sys.exit(0 if result["status"] == "PASS" else 1)
