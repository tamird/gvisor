#!/usr/bin/env python3
"""Run the existing bridge fixture with a verified distribution of runsc."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

RELEASES = {
    "current": ("20261102.0-22-g471d6dff5229", "9a30c8c48fcbf90b7f67fcf92abb77dd61d738c34f5e94a1937d40e001c39dec"),
    "july": ("20260727.0", "386bdc2196fc600b68ff8dafdd8ecdc6a8e033ecf9cfa9dfab61ec1b389da307"),
}
BIN_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
TOOLS = ("bash", "sh", "dirname", "readlink", "grep", "which", "mount", "cp", "mkdir",
         "ip", "python3", "seq", "sleep", "cat")
if sys.argv[1:] == ["--tools"]:
    # This process runs INSIDE the actual Sentry, before any of the traffic cases.
    import collections, errno, ipaddress, select, socket, struct, threading, time
    resolved = {}
    for name in TOOLS:
        p = shutil.which(name)
        if not p or not os.access(os.path.realpath(p), os.X_OK):
            raise ValueError("missing guest utility: " + name)
        resolved[name] = [p, os.path.realpath(p)]
    assert subprocess.check_output(["which", "mount"], text=True).strip() == resolved["mount"][0]
    for argv in (["ip", "-V"], ["mount", "--version"], ["bash", "--version"]):
        subprocess.run(argv, stdout=subprocess.DEVNULL, check=True)
    print("GUEST16908 " + json.dumps(resolved), flush=True)
    raise SystemExit(0)

FILES = Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"] / "test/rtnetlink/linux"
WORK = Path(os.environ["TEST_TMPDIR"]).resolve()
ENV = {"PATH": BIN_PATH, "LC_ALL": "C"}


def digest(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def text(*args):
    return subprocess.check_output(args, text=True, env=ENV, stderr=subprocess.STDOUT).strip()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def diagnose(raw):
    lines = []
    for line in raw.splitlines():
        if not re.search(r"(error|fail|unsupported|not permitted|denied|not found|no such|timeout|cannot|exec format|read.only|traceback|unexpected)", line, re.I):
            continue
        if re.search(r"token|password|secret|api.key|authorization|bearer|https?://|g[hp]s?_", line, re.I):
            continue
        if len(lines) < 16:
            lines.append(line[:280])
    return lines


def run_cell(runtime, variant):
    work = WORK / variant
    root = work / "rootfs"
    root.mkdir(parents=True)
    for name in ("usr", "tmp", "run", "var", "etc", "proc", "sys", "dev", "fixture"):
        (root / name).mkdir()
    for name in ("bin", "sbin", "lib", "lib64"):
        (root / name).symlink_to("usr/" + name)
    (root / "var/run").symlink_to("../run")
    (root / "etc/hosts").write_text("127.0.0.1 localhost\n192.168.0.2 srv\n")
    (root / "etc/nsswitch.conf").write_text("hosts: files\n")
    (root / "etc/alternatives").mkdir()
    for name in (TOOLS + ("sed",) if variant == "source-veth" else TOOLS):
        found = shutil.which(name, path=BIN_PATH)
        require(found is not None, "missing host fixture utility: " + name)
        p = Path(found)
        final = p.resolve(strict=True)
        require(final.is_relative_to("/usr") and final.is_file(), "invalid host utility: " + name)
        visited = set()
        while p.is_symlink():
            require(p not in visited, "cyclic host utility")
            visited.add(p)
            if p.parent == Path("/etc/alternatives"):
                dest = root / "etc/alternatives" / p.name
                if not dest.is_symlink():
                    dest.symlink_to(final)
            p = Path(os.path.normpath(p.parent / p.readlink()))
    fixture = root / "fixture"
    inputs = ("setlink_test.sh", "rtnetlink_test.sh") if variant == "source-veth" else ("bridge_test.sh", "short_connections.py", "rtnetlink_test.sh")
    for name in inputs:
        source = name
        if variant in ("baseline", "tools"):
            source = {"bridge_test.sh": "baseline-bridge.sh", "short_connections.py": "baseline-short.py"}.get(name, name)
        shutil.copyfile(FILES / source, fixture / name)
    # No customer, host home, workspace, or network is mounted in the sandbox.
    mounts = [dict(destination="/usr", type="bind", source="/usr", options=["rbind", "ro", "nosuid", "nodev"])]
    for destination, kind in (("/proc", "proc"), ("/sys", "sysfs"), ("/tmp", "tmpfs"), ("/run", "tmpfs")):
        mounts.append(dict(destination=destination, type=kind, source=kind))
    caps = ["CAP_SYS_ADMIN", "CAP_NET_ADMIN", "CAP_NET_RAW", "CAP_DAC_OVERRIDE"]
    env = ["PATH=" + BIN_PATH, "LC_ALL=C", "TEST_ON_GVISOR=1"]
    if variant.startswith("forward"):
        env.append("TCP16908_FORWARD=" + variant[-1])
    args = ["/bin/bash", "/fixture/setlink_test.sh" if variant == "source-veth" else "/fixture/bridge_test.sh"]
    if variant == "tools":
        shutil.copyfile(Path(__file__).resolve(), fixture / "check.py")
        args = ["/usr/bin/python3", "/fixture/check.py", "--tools"]
    spec = dict(ociVersion="1.0.2", root=dict(path="rootfs", readonly=True),
                process=dict(args=args, cwd="/", user=dict(uid=0, gid=0),
                             env=env, capabilities={k: caps for k in ("effective", "permitted", "bounding")}),
                mounts=mounts, linux=dict(namespaces=[dict(type=k) for k in ("pid", "network", "ipc", "uts", "mount")]))
    (work / "config.json").write_text(json.dumps(spec))
    # Only the sandbox launcher gets sudo, and only package binaries verified below can be run.
    row = dict(variant=variant, cells=[], fixture={p.name:digest(p) for p in fixture.iterdir()})
    # A short, private tmpfs keeps runsc's Unix socket below the sockaddr limit
    # and never leaves root-owned files in Bazel's output directories.
    with tempfile.TemporaryDirectory(prefix="tcp16908-", dir="/tmp") as state:
        args = ["/usr/bin/sudo", "-n", "/usr/bin/unshare", "--mount", "--net", "--propagation", "private",
                "/bin/bash", "-ec", 'mount -t tmpfs -o mode=0700 tmpfs "$1"; shift; exec "$@"', "bridge", state,
                "/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "LC_ALL=C", runtime,
                "--root=" + state, "--platform=systrap", "--network=none", "--net-raw=true",
                "--ignore-cgroups=true", "run", "--bundle=" + str(work), "shortbridge"]
        try:
            p = subprocess.run(args, text=True, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=270)
            row["runtime_exit"] = p.returncode
            output = p.stdout
        except subprocess.TimeoutExpired as exc:
            row["timeout"] = True
            output = (exc.stdout or b"").decode(errors="replace")
    if variant == "source-veth":
        # Keep the original shell trace in the local Bazel report. Only the
        # fixed MTU and error projections are published from that report.
        print(output, end="", flush=True)
    row["diagnostics"] = diagnose(output)
    for line in output.splitlines():
        if line.startswith("TCP16908 {"):
            row["cells"].append(json.loads(line.split(" ", 1)[1]))
        if line.startswith("GUEST16908 {"):
            row["tools"] = json.loads(line.split(" ", 1)[1])
    return row


def main():
    name, = sys.argv[1:]
    if name == "source-veth":
        runtime_tree = WORK / "release"
        shutil.copytree(FILES / "veth-release", runtime_tree)
        runtime = runtime_tree / "runsc"
        for path in (runtime, runtime_tree / "gvisor-bin/gvisor_sentry"):
            print("VETH_BINARY " + path.name + " " + digest(path), flush=True)
        result = run_cell(runtime, name)
        return result.get("runtime_exit", 1)
    version, expected = RELEASES[name]
    record = {"release": name, "package_sha256": None, "variants": []}
    try:
        package = FILES / (name + ".deb")
        record["package_sha256"] = digest(package)
        require(record["package_sha256"] == expected and text("dpkg-deb", "-f", package, "Version") == version
                and text("dpkg-deb", "-f", package, "Architecture") == "amd64", "wrong package")
        tree = WORK / "release"
        subprocess.run(["dpkg-deb", "-x", package, tree], env=ENV, check=True)
        runtime = tree / "usr/bin/runsc"
        sidecars = sorted((runtime.parent / "gvisor-bin").glob("*"))
        require(runtime.is_file() and sidecars and all(p.is_file() and p.resolve().is_relative_to(tree) for p in [runtime, *sidecars]),
                "missing package executable")
        record["binaries"] = {str(p.relative_to(tree)):digest(p) for p in [runtime, *sidecars]}
        embedded = text(runtime, "--version").splitlines()[0]
        record["version"] = embedded
        require(embedded in ("runsc version " + version, "runsc version release-" + version), "wrong embedded version")
        focus = os.environ.get("SHORT_BRIDGE_FOCUS") == "1"
        modes = ("tools",) if os.environ.get("SHORT_BRIDGE_TOOLS") == "1" else ("forward1",) if focus else ("baseline",)
        for variant in modes:
            record["variants"].append(run_cell(runtime, variant))
    except Exception as exc:
        record["error"] = str(exc) if type(exc) is ValueError else type(exc).__name__
        if isinstance(exc, subprocess.CalledProcessError):
            record["setup_exit"] = exc.returncode
            record["setup_diagnostics"] = diagnose(exc.output or "") if isinstance(exc.output,str) else []
    finally:
        print("PACKAGED16908 " + json.dumps(record, separators=(",", ":")), flush=True)
    return int("error" in record or any(r.get("runtime_exit") != 0 for r in record["variants"]))


if __name__ == "__main__":
    raise SystemExit(main())
