"""Exercise repository configuration using the existing stripped Linux client."""
from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile


SOURCE = "d5fe3c2eef21434cbe13e3604f0f070ace4d7edb"
BAZEL = "7f775b54bbec487efaf0f49ccda1d2ed02cd34f582edfdd6aeeec76e39ef585c"
STAGE = Path(os.environ["RUNNER_TEMP"]) / "lean-client"
RECEIPT = STAGE.parent / "lean-client.json"
TARGETS = [
    ("query", "@org_golang_x_sys//unix:go_default_library"),
    ("cquery", "//:release"),
]
POSSIBLE = {"bash", "sh", "sed", "test", "cp", "chmod", "uname", "python3",
            "mkdir", "cat", "readlink", "tar", "git", "rm", "mv", "ln"}
report = {"source": SOURCE, "bazel": BAZEL, "known_client_tool": "/usr/bin/env", "phases": []}


def run(*args, **opts):
    return subprocess.run(args, check=True, **opts)


def write_report():
    RECEIPT.write_text(json.dumps(report, indent=2) + "\n")


def setup():
    assert subprocess.check_output(["git", "rev-parse", "HEAD^"], text=True).strip() == SOURCE
    assert Path(".bazelversion").read_text().strip() == "9.3.1"
    STAGE.mkdir()
    image = STAGE / "image"
    image.mkdir()
    probe = image / "probe/bazel"
    probe.parent.mkdir()
    run("curl", "-fsSL", "--retry", "3", "https://releases.bazel.build/9.3.1/release/bazel-9.3.1-linux-x86_64", "-o", probe)
    assert hashlib.sha256(probe.read_bytes()).hexdigest() == BAZEL
    probe.chmod(0o755)
    # Same native launcher/JVM libraries and CA certificates as the original
    # Gazelle minimum-client qualification. No host /usr or /bin mounts.
    for directory in ("etc", "bin", "usr/bin", "home/runner", "work", "out", "cache", "data", "tmp"):
        (image / directory).mkdir(parents=True, exist_ok=True)
    def add(src, dest=None):
        out = image / str(dest or src).lstrip("/")
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
    for lib in ("ld-linux-x86-64.so.2", "libc.so.6", "libm.so.6", "libz.so.1",
                "libdl.so.2", "libpthread.so.0", "libgcc_s.so.1", "libstdc++.so.6",
                "libresolv.so.2", "librt.so.1", "libutil.so.1", "libnss_files.so.2",
                "libnss_dns.so.2", "libtinfo.so.6", "libcrypt.so.1", "libzstd.so.1"):
        path = Path("/lib/x86_64-linux-gnu") / lib
        if path.is_file():
            add(path)
    add("/lib64/ld-linux-x86-64.so.2")
    add("/usr/bin/env")
    add("/etc/ssl/certs/ca-certificates.crt")
    add("/etc/ssl/certs/java/cacerts", "/trust/cacerts")
    (image / "etc/passwd").write_text(f"runner:x:{os.getuid()}:{os.getgid()}:runner:/home/runner:/nonexistent\n")
    (image / "etc/group").write_text(f"runner:x:{os.getgid()}:\n")
    (image / "etc/nsswitch.conf").write_text("passwd: files\ngroup: files\nhosts: files dns\n")
    assert sorted(p.name for p in (image / "usr/bin").iterdir()) == ["env"]
    assert not list((image / "bin").iterdir())
    with tarfile.open(STAGE / "image.tar", "w") as tar:
        tar.add(image, arcname=".")
    run("docker", "import", STAGE / "image.tar", "lean-client:probe", stdout=subprocess.DEVNULL)
    for name in ("work", "out", "cache", "data", "home"):
        (STAGE / name).mkdir()
    with subprocess.Popen(["git", "archive", SOURCE], stdout=subprocess.PIPE) as proc:
        run("tar", "-xf", "-", "-C", STAGE / "work", stdin=proc.stdout)
        assert proc.wait() == 0


def varint(reader):
    value = shift = 0
    while part := reader.read(1):
        value |= (part[0] & 127) << shift
        if part[0] < 128:
            return value
        shift += 7
        assert shift < 70
    raise EOFError


def fields(data):
    reader = io.BytesIO(data)
    while reader.tell() < len(data):
        key = varint(reader)
        code = key & 7
        if code == 2:
            yield key >> 3, reader.read(varint(reader))
        elif code == 0:
            varint(reader)
        else:
            reader.read({1: 8, 5: 4}[code])


def workspace_operations(path):
    # WorkspaceEvent field numbers from Bazel 9.3.1's workspace_log.proto.
    # Do not persist command arguments, environments, URLs or repository data.
    counter = Counter()
    with path.open("rb") as f:
        while True:
            try:
                size = varint(f)
            except EOFError:
                break
            record = dict(fields(f.read(size)))
            repo = record.get(2, b"").decode(errors="replace")
            if not re.fullmatch(r"[a-zA-Z0-9_@+:./ -]{1,220}", repo):
                continue
            for kind, field, attr in (("exec", 3, 2), ("which", 10, 1)):
                if field in record:
                    value = next((v.decode(errors="replace") for k, v in fields(record[field]) if k == attr), "")
                    name = os.path.basename(value)
                    if re.fullmatch(r"[A-Za-z0-9._+-]{1,70}", name):
                        origin = "path" if "/" not in value else "system" if value.startswith(("/bin/", "/usr/bin/")) else "other"
                        counter[(repo, kind, origin, name)] += 1
    return [list(key) + [count] for key, count in counter.most_common(180)]


def phase(index, command, target, tool=None):
    args = ["docker", "run", "--rm", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--read-only", "--tmpfs", "/tmp:rw,exec,mode=1777", "--user", f"{os.getuid()}:{os.getgid()}",
            "--workdir", "/work", "-e", "HOME=/home/runner", "-e", "PATH=/usr/bin:/bin"]
    for directory, dest in (("work", "/work"), ("out", "/out"), ("cache", "/cache"), ("data", "/data"), ("home", "/home/runner")):
        args += ["-v", f"{STAGE/directory}:{dest}"]
    if tool:
        path = Path("/usr/bin") / tool
        assert tool in POSSIBLE and path.is_file()
        args += ["-v", f"{path.resolve()}:/usr/bin/{tool}:ro", "-v", f"{path.resolve()}:/bin/{tool}:ro"]
    args += ["lean-client:probe", "/probe/bazel", "--batch", "--nosystem_rc", "--nohome_rc",
             "--output_user_root=/out", f"--output_base=/out/{'control' if tool else 'minimal'}",
             "--host_jvm_args=-Djavax.net.ssl.trustStore=/trust/cacerts", command,
             "--repository_cache=/cache", "--color=no", "--curses=no", "--output=label",
             f"--experimental_workspace_rules_log_file=/data/{index}.pb"]
    if command == "cquery":
        args += ["--config=x86_64", "-c", "opt", "--symlink_prefix=/"]
    args.append(target)
    result = subprocess.run(args, capture_output=True, timeout=1100)
    text = result.stderr.decode(errors="replace")
    lines = []
    for line in text.splitlines():
        if re.search(r"(?i)token|secret|api.key|authorization|cookie|password|BEARER|-----BEGIN", line):
            continue
        if re.search(r"(?i)error|failed|cannot|missing|traceback|\.bzl.+line [0-9]|no such|not found|permission denied|execvp|execve|return code|no matching|not a directory|disk space", line):
            safe = re.sub(r"https?://\S+", "[public URL]", line)
            lines.append(safe[:550])
    row = {"command": command, "target": target, "only_added_tool": tool, "exit": result.returncode,
           "stdout_labels": [s for s in result.stdout.decode(errors="replace").splitlines()
                             if re.fullmatch(r"[@a-zA-Z0-9._+: /()-]{1,200}", s)][:4],
           "diagnostics": list(dict.fromkeys(lines))[-32:]}
    report["phases"].append(row)
    write_report()
    path = STAGE / "data" / f"{index}.pb"
    if path.is_file():
        row["repository_calls"] = workspace_operations(path)
    write_report()
    return row


try:
    setup()
    for command, target in TARGETS:
        row = phase(len(report["phases"]), command, target)
        if row["exit"]:
            diagnostic = "\n".join(row["diagnostics"])
            missing = {word for word in POSSIBLE if re.search(r"(?i)(?:execvp\(|cannot run program [\"']|/usr/bin/|/bin/|(?:executable|command|program)[ :\x60'\"(]+)" + word +
                        r"(?:[\"'):\x60]|\s+(?:not found|failed))", diagnostic)}
            if len(missing) == 1:
                phase(len(report["phases"]), command, target, missing.pop())
            break
except (AssertionError, OSError, ValueError, EOFError, subprocess.SubprocessError) as exc:
    report["harness_error"] = type(exc).__name__
    raise
finally:
    write_report()
