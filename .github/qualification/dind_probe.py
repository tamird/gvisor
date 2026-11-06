import hashlib
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import tarfile
import time


def output(*cmd, **kwargs):
    return subprocess.check_output(cmd, text=True, **kwargs).strip()


def inspect_connection(pid, address):
    # Run in dockerd's mount and network namespaces. SO_PEERCRED identifies
    # the listening process. Match a separately established dockerd socket to
    # an FD held by that process, so a reachable listener is not sufficient.
    pid = int(pid)
    with socket.socket(socket.AF_UNIX) as probe:
        probe.settimeout(5)
        probe.connect(address)
        server, _, _ = struct.unpack("3i", probe.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    server_fds = set()
    for fd in Path(f"/proc/{server}/fd").iterdir():
        try:
            name = os.readlink(fd)
        except FileNotFoundError:
            continue
        if match := re.fullmatch(r"socket:\[(\d+)\]", name):
            server_fds.add(match[1])
    rows = output("ss", "-H", "-xanp").splitlines()
    dockerd = [r for r in rows if f"pid={pid}," in r and "ESTAB" in r]
    edges = []
    for row in dockerd:
        cols = row.split()
        if len(cols) >= 8 and cols[7] in server_fds:
            edges.append({"local_inode": cols[5], "containerd_inode": cols[7]})
    return {"socket": address, "dockerd_pid": pid, "containerd_pid": server,
            "dockerd_mnt": os.readlink(f"/proc/{pid}/ns/mnt"),
            "containerd_mnt": os.readlink(f"/proc/{server}/ns/mnt"),
            "server": Path(f"/proc/{server}/comm").read_text().strip(),
            "connections": edges, "observed_client_sockets": [s[:350] for s in dockerd[:12]]}


def peer_receipt(pidfile, address):
    pid = pidfile.read_text().strip()
    return json.loads(output("sudo", "-n", "nsenter", "--target", pid, "--mount", "--net",
                             sys.executable, "-B", str(Path(__file__).resolve()), "inspect", pid, str(address)))


def daemon_argv(binaries, directory, config, isolated, name):
    # The host containerd keeps running. Hide its socket only in this
    # daemon's already private mount namespace, for the managed case.
    launcher = 'ip link set lo up; '
    if isolated:
        launcher += 'mount -t tmpfs -o mode=755 tmpfs /run/containerd; '
    return ["sudo", "-n", "unshare", "--mount", "--net", "--propagation", "private",
            "/bin/sh", "-ec", launcher + 'exec "$@"', "run-daemon",
            "env", "PATH=" + str(binaries) + ":" + os.environ["PATH"], str(binaries / "dockerd"),
            "--config-file", str(config), "--host", "unix://" + str(directory / "docker.sock"),
            "--pidfile", str(directory / "docker.pid"), "--data-root", str(directory / "data"),
            "--exec-root", str(directory / "exec"), "--containerd-namespace", name,
            "--containerd-plugins-namespace", name + "-plugins"]


def exported_files(cli, env, container):
    paths = {"bin/sh", "bin/busybox", "lib/ld-musl-x86_64.so.1"}
    found = {}
    with subprocess.Popen([cli, "export", container], stdout=subprocess.PIPE, env=env) as export:
        with tarfile.open(fileobj=export.stdout, mode="r|*") as archive:
            for m in archive:
                name = m.name.removeprefix("./")
                if name not in paths:
                    continue
                record = {"link": m.linkname, "mode": oct(m.mode), "size": m.size}
                if m.isfile() and m.size < 8_000_000:
                    data = archive.extractfile(m).read()
                    record["sha256"] = hashlib.sha256(data).hexdigest()
                    if data.startswith(b"\x7fELF\x02\x01"):
                        record["machine"] = struct.unpack_from("<H", data, 18)[0]
                        phoff, = struct.unpack_from("<Q", data, 32)
                        entsize, count = struct.unpack_from("<HH", data, 54)
                        for i in range(count):
                            typ, _, offset, _, _, length, _, _ = struct.unpack_from("<IIQQQQQQ", data, phoff + i * entsize)
                            if typ == 3:
                                record["interpreter"] = data[offset:offset+length].rstrip(b"\0").decode()
                found[name] = record
        if export.wait():
            raise RuntimeError("docker export failed")
    return found


def run_mode(root, name, isolated, report, save):
    d = root / name
    d.mkdir()
    binaries = root / "docker27"
    cli = binaries / "docker"
    pidfile = d / "docker.pid"
    config = d / "daemon.json"
    config.write_text(json.dumps({"experimental": True, "storage-driver": "overlay2"}))
    env = dict(os.environ, DOCKER_HOST="unix://" + str(d / "docker.sock"))
    def docker(*args):
        return output(cli, *args, env=env)
    with (d / "daemon.log").open("w") as log:
        daemon = subprocess.Popen(daemon_argv(binaries, d, config, isolated, name), stdout=log, stderr=log)
    cid = None
    try:
        for _ in range(90):
            if daemon.poll() is not None:
                raise RuntimeError(f"{name}: dockerd exited before info")
            if (d / "docker.sock").exists() and subprocess.run([cli, "info"], env=env, capture_output=True).returncode == 0:
                break
            time.sleep(1)
        report["version"] = docker("version", "--format", "{{.Server.Version}}")
        assert report["version"] == "27.5.1"
        docker("load", "-i", root / "moby.tar")
        tag = "gvisor.dev/images/moby:latest"
        report["image"] = docker("image", "inspect", "-f", "{{.Id}}", tag)
        assert report["image"] == "sha256:fcae5dc4a1e60d712605f3dcdd99292c9e6ba2a4d17c4b4e8a0e62b7993b66f4"
        cid = docker("create", "--network", "none", "--runtime", "runc", "--workdir", "/",
                     "--entrypoint", "/bin/sh", tag, "-c", "printf 'dind-native-shell-ok\\n'")
        report["export"] = exported_files(cli, env, cid)
        save()
        run = subprocess.run([cli, "start", "-a", cid], text=True, capture_output=True, env=env)
        report["start"] = {"exit": run.returncode, "stdout": run.stdout[:800], "stderr": run.stderr[:1200]}
        save()
        address = d / "exec/containerd/containerd.sock" if isolated else Path("/run/containerd/containerd.sock")
        report["containerd"] = peer_receipt(pidfile, address)
        save()
    finally:
        if cid:
            subprocess.run([cli, "rm", "-fv", cid], capture_output=True, env=env)
        subprocess.run([cli, "image", "rm", "gvisor.dev/images/moby:latest"], capture_output=True, env=env)
        if pidfile.exists():
            subprocess.run(["sudo", "-n", "kill", "-TERM", pidfile.read_text().strip()])
        try:
            daemon.wait(timeout=30)
        except subprocess.TimeoutExpired:
            daemon.terminate()
        if (d / "daemon.log").exists():
            report["daemon_diagnostics"] = [l[:500] for l in (d / "daemon.log").read_text(errors="replace").splitlines()
                if "starting managed containerd" in l or "failed to start" in l or "level=error" in l][-8:]
        save()


def controls():
    root = Path(os.environ["RUNNER_TEMP"]) / "dind-stream"
    evidence = {"modes": {}, "distinguished": False}
    def save():
        (root / "control.json").write_text(json.dumps(evidence, indent=2) + "\n")
    save()
    for name, isolated in (("dind-external", False), ("dind-managed", True)):
        result = evidence["modes"][name] = {}
        try:
            run_mode(root, name, isolated, result, save)
        except Exception as err:
            result["error"] = str(err)[:600]
            save()
    if all("containerd" in r for r in evidence["modes"].values()):
        ext, owned = evidence["modes"].values()
        peers = [r["containerd"] for r in (ext, owned)]
        fs = owned["export"]
        error = ext["start"]["stderr"]
        evidence["distinguished"] = bool(
            ext["export"] == fs
            and fs["bin/sh"]["link"] == "/bin/busybox"
            and fs["bin/busybox"]["machine"] == fs["lib/ld-musl-x86_64.so.1"]["machine"] == 62
            and fs["bin/busybox"]["interpreter"] == "/lib/ld-musl-x86_64.so.1"
            and all(p["server"] == "containerd" and p["connections"] for p in peers)
            and peers[0]["dockerd_mnt"] != peers[0]["containerd_mnt"]
            and peers[1]["dockerd_mnt"] == peers[1]["containerd_mnt"]
            and ext["start"]["exit"] != 0 and "/bin/sh" in error and "no such file" in error
            and owned["start"]["exit"] == 0 and owned["start"]["stdout"].strip() == "dind-native-shell-ok")
    save()
    print("runc A/B distinguished:", evidence["distinguished"])


if __name__ == "__main__":
    if sys.argv[1] == "inspect":
        print(json.dumps(inspect_connection(sys.argv[2], sys.argv[3])))
    else:
        controls()
