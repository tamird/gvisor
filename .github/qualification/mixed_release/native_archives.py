"""Compare the stamp, nine installed payloads and container bytes of all six releases."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import platform
import re
import subprocess
import sys
import tarfile

# This script runs with -I and imports only the source-bound copied helper.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import os
D = Path(os.environ["RUNNER_TEMP"]) / "mixed-publication"
STAMP = "release-20261106.0"

MEMBERS = {"runsc", "containerd-shim-runsc-v1"} | {"gvisor-bin/" + name for name in (
    "gvisor_sentry", "checkpointgofer", "gvisor-sentry-prewarmer",
    "runsc-fd-parking", "runsc-metric-server", "runsc-netgofer", "gvisor-proc-umounter")}
EXPECTED_ISSUES = (KeyError, ValueError, TypeError, AssertionError, OSError, tarfile.TarError, subprocess.SubprocessError)


def sha(path):
    with path.open("rb") as f:
        h = hashlib.sha256()
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
        return h.hexdigest()


@contextmanager
def stream(path):
    if path.name.endswith("bz2"):
        with tarfile.open(path, "r|bz2") as tar:
            yield tar
        return
    args = ["dpkg-deb", "--fsys-tarfile", str(path)] if path.suffix == ".deb" else ["zstd", "-qdc"]
    # zstd's default name handling treats Bazel's .tar.zstd differently from a
    # .tar.zst filename. Supply bytes on stdin as in the prior release check.
    with path.open("rb") as source, subprocess.Popen(args, stdin=source, stdout=subprocess.PIPE,
                                                      stderr=subprocess.DEVNULL) as process:
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|") as tar:
                yield tar
            while process.stdout.read(1 << 20):
                pass
            assert process.wait() == 0
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()


def deb_control(path, arch, row):
    wanted = {"Package": "runsc", "Version": STAMP.removeprefix("release-"), "Architecture": arch}
    for k, v in wanted.items():
        actual = subprocess.check_output(["dpkg-deb", "--field", path, k], text=True).strip()
        row[k] = actual == v
    with subprocess.Popen(["dpkg-deb", "--ctrl-tarfile", path],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for entry in tar:
                name = entry.name.removeprefix("./")
                if name not in ("changelog", "postinst", "conffiles"):
                    continue
                assert entry.isreg() and entry.size < 65536
                data = tar.extractfile(entry).read()
                if name == "changelog":
                    lines = data.decode().splitlines()
                    row[name] = lines[0] == "runsc (20261106.0) unstable; urgency=medium" and lines[-1].endswith("Fri, 06 Nov 2026 00:00:00 +0000")
                elif name == "postinst":
                    row[name] = entry.mode == 0o755 and data == Path("debian/postinst.sh").read_bytes()
                else:
                    row[name] = data.strip() == b"/etc/containerd/runsc.toml"
        proc.stdout.read()
        assert proc.wait() == 0
    return set(row) == set(wanted) | {"changelog", "postinst", "conffiles"} and all(row.values())


def archive(path, arch, row):
    row.update(bytes=path.stat().st_size, sha256=sha(path), files={})
    deb = path.suffix == ".deb"
    machine = {"amd64": 62, "arm64": 183}[arch]
    save_cli = deb and arch == "amd64" and platform.machine() == "x86_64"
    with stream(path) as tar:
        for entry in tar:
            name = entry.name.removeprefix("./")
            if entry.isdir():
                continue
            if deb and name == "etc/containerd/runsc.toml":
                row["config"] = (entry.isreg() and entry.mode == 0o644 and entry.size < 65536
                                 and tar.extractfile(entry).read() == Path("shim/runsc.toml").read_bytes())
                continue
            if deb and not name.startswith("usr/bin/"):
                raise ValueError("unexpected deb payload")
            if deb:
                name = name.removeprefix("usr/bin/")
            assert name in MEMBERS and name not in row["files"] and entry.isreg()
            assert 0 < entry.size < 300 << 20 and entry.mode & 0o7777 == 0o755
            with tar.extractfile(entry) as f:
                header = f.read(64)
                assert len(header) == 64 and header[:7] == b"\x7fELF\x02\x01\x01"
                em = int.from_bytes(header[18:20], "little")
                assert em == machine
                digest = hashlib.sha256(header)
                tail = header
                has_label = False
                cli = D / "version-cli" if save_cli and name == "runsc" else None
                copy = cli.open("wb") if cli else None
                try:
                    if copy:
                        copy.write(header)
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        digest.update(chunk)
                        if name in ("runsc", "gvisor-bin/gvisor_sentry") and not has_label:
                            has_label = STAMP.encode() in tail + chunk
                            tail = chunk[-len(STAMP):]
                        if copy:
                            copy.write(chunk)
                finally:
                    if copy:
                        copy.close()
                if cli:
                    cli.chmod(0o700)
                    try:
                        stdout = subprocess.check_output([cli, "--version"], text=True, timeout=15)
                        row["native_cli_stamp"] = stdout.splitlines()[0] == "runsc version " + STAMP
                    finally:
                        cli.unlink()
                if name in ("runsc", "gvisor-bin/gvisor_sentry"):
                    row.setdefault("linked_label", {})[name] = has_label
                row["files"][name] = {"sha256": digest.hexdigest(), "bytes": entry.size,
                                      "mode": entry.mode & 0o7777, "machine": em}
    control_ok = True
    if deb:
        row["control"] = {}
        control_ok = deb_control(path, arch, row["control"]) and row.get("config") is True
    return (set(row["files"]) == MEMBERS and all(row["linked_label"].values()) and control_ok
            and (not save_cli or row.get("native_cli_stamp") is True))
