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
FIXED = "ab32faca2351caa03d67f5463d3b17c3307f8be4"
BAZEL = "7f775b54bbec487efaf0f49ccda1d2ed02cd34f582edfdd6aeeec76e39ef585c"
STAGE = Path(os.environ["RUNNER_TEMP"]) / "lean-client"
RECEIPT = STAGE.parent / "lean-client.json"
TARGETS = [
    ("cquery", "set(//:release @org_golang_x_sys//unix:go_default_library)"),
]
report = {"base": SOURCE, "fixed": FIXED, "bazel": BAZEL, "path_files": [], "phases": []}


def run(*args, **opts):
    return subprocess.run(args, check=True, **opts)


def write_report():
    RECEIPT.write_text(json.dumps(report, indent=2) + "\n")


def setup():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
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
    for directory in ("etc", "bin", "usr/bin", "home/runner/.cache", "data", "tmp"):
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
    add("/etc/ssl/certs/ca-certificates.crt")
    # The JVM's filename encoding follows the active glibc locale. Both arms
    # receive the same locale data; only the locale selection can differ.
    for data in Path("/usr/lib/locale/C.utf8").glob("**/*"):
        if data.is_file():
            add(data)
    for java_trust in (Path("/etc/ssl/certs/java/cacerts"),
                       Path(os.environ.get("JAVA_HOME", "/usr/lib/jvm/default-java")) / "lib/security/cacerts"):
        if java_trust.is_file():
            add(java_trust, "/trust/cacerts")
            break
    (image / "etc/passwd").write_text(f"runner:x:{os.getuid()}:{os.getgid()}:runner:/home/runner:/nonexistent\n")
    (image / "etc/group").write_text(f"runner:x:{os.getgid()}:\n")
    (image / "etc/nsswitch.conf").write_text("passwd: files\ngroup: files\nhosts: files dns\n")
    assert not list((image / "usr/bin").iterdir())
    assert not list((image / "bin").iterdir())
    with tarfile.open(STAGE / "image.tar", "w") as tar:
        tar.add(image, arcname=".")
    run("docker", "import", STAGE / "image.tar", "lean-client:probe", stdout=subprocess.DEVNULL)
    (STAGE / "data").mkdir()


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
    programs = Counter()
    for (_, kind, origin, executable), count in counter.items():
        programs[(kind, origin, executable)] += count
    return {"programs": [list(key) + [count] for key, count in programs.most_common()],
            "owners": [list(key) + [count] for key, count in counter.most_common(40)]}


def phase(index, command, target, source):
    work = Path.cwd()
    run("git", "switch", "--detach", source)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == source
    # The clean git checkout has no tracked module lock. Discard only a lock
    # created by a previous probe so both cases start on the identical source.
    if (work / "MODULE.bazel.lock").exists():
        assert not subprocess.check_output(["git", "ls-files", "MODULE.bazel.lock"])
        (work / "MODULE.bazel.lock").unlink()
    run("git", "diff", "--quiet", "HEAD", "--")
    args = ["docker", "run", "--rm", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--read-only", "--tmpfs", "/tmp:rw,exec,mode=1777", "--user", f"{os.getuid()}:{os.getgid()}",
            "--workdir", str(work), "-e", "HOME=/home/runner", "-e", "PATH=/usr/bin:/bin",
            "--mount", "type=volume,dst=/home/runner", "-v", f"{work}:{work}", "-v", f"{STAGE/'data'}:/data"]
    assert (STAGE / "image/usr/lib/locale/C.utf8/LC_CTYPE").is_file()
    args += ["lean-client:probe", "/probe/bazel", "--batch", "--nosystem_rc", "--nohome_rc"]
    if (STAGE / "image/trust/cacerts").is_file():
        args += ["--host_jvm_args=-Djavax.net.ssl.trustStore=/trust/cacerts"]
    args += [command, "--color=no", "--curses=no", "--output=label",
             f"--experimental_workspace_rules_log_file=/data/{index}.pb"]
    if command == "cquery":
        args += ["--config=x86_64", "-c", "opt", "--symlink_prefix=/",
                 "--extra_toolchains=//test/rbe/go_bootstrap:linux_amd64_ash"]
    args.append(target)
    result = subprocess.run(args, capture_output=True, timeout=1100)
    text = result.stderr.decode(errors="replace")
    lines = []
    context = 0
    for line in text.splitlines():
        if re.search(r"(?i)token|secret|api.key|authorization|cookie|password|BEARER|-----BEGIN", line):
            continue
        if "matching toolchains" in line or "toolchain resolution" in line:
            context = 5
        if context or re.search(r"(?i)error|failed|cannot|missing|traceback|\.bzl.+line [0-9]|no such|not found|permission denied|execvp|execve|return code|no matching|not a directory|disk space|unsupported encoding|UTF-8 locale", line):
            safe = re.sub(r"https?://\S+", "[public URL]", line)
            safe = re.sub(r"/home/runner/\.cache/bazel/_bazel_runner/[0-9a-f]{32}/external/", "@@", safe)
            lines.append(safe if len(safe) <= 1150 else safe[:350] + " ... " + safe[-750:])
            context = max(0, context-1)
    row = {"command": command, "target": target, "source": source, "declared_shell": "//test/rbe/go_bootstrap:linux_amd64_ash", "exit": result.returncode,
           "stdout_labels": [s for s in result.stdout.decode(errors="replace").splitlines()
                             if re.fullmatch(r"[@a-zA-Z0-9._+: /()-]{1,200}", s)][:4],
           "diagnostics": list(dict.fromkeys(lines[:8] + lines[-28:]))}
    report["phases"].append(row)
    write_report()
    path = STAGE / "data" / f"{index}.pb"
    if path.is_file():
        row["repository_calls"] = workspace_operations(path)
    write_report()
    return row


try:
    setup()
    for source in (SOURCE, FIXED):
        for command, target in TARGETS:
            phase(len(report["phases"]), command, target, source)
    original, fixed = report["phases"]
    report["observed"] = {
        "original_requires_env": original["exit"] != 0 and any(
            "env" in line.lower() and any(marker in line.lower() for marker in ("error", "no such file", "execvp", "not found"))
            for line in original["diagnostics"]
        ),
        "release_configured": fixed["exit"] == 0 and any(label.startswith("//:release (") for label in fixed["stdout_labels"]),
        "dependency_configured": fixed["exit"] == 0 and any(label.startswith("@org_golang_x_sys//unix:") for label in fixed["stdout_labels"]),
    }
except (AssertionError, OSError, ValueError, EOFError, subprocess.SubprocessError) as exc:
    report["harness_error"] = type(exc).__name__
    raise
finally:
    write_report()
