"""Accept only regular compiler outputs; copy the validated payload into the trusted build."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tarfile


@contextmanager
def regular(path, maximum):
    # Docker --rm returned before the host inspects these paths. O_NOFOLLOW
    # independently binds each read to the regular inode approved by lstat.
    recorded = path.lstat()
    if not stat.S_ISREG(recorded.st_mode) or recorded.st_nlink != 1 or not 0 < recorded.st_size <= maximum:
        raise ValueError("invalid compiler output")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        actual = os.fstat(stream.fileno())
        if not os.path.samestat(recorded, actual) or actual.st_size != recorded.st_size:
            raise ValueError("compiler output changed")
        yield stream


def original_exit(directory, name):
    with regular(directory / "output" / (name + ".exit"), 5) as stream:
        code = stream.read()
    if re.fullmatch(rb"(?:0|[1-9][0-9]{0,2})\n", code) is None or int(code) > 255:
        raise ValueError("invalid Bazel exit")
    (directory / (name + ".exit")).write_bytes(code)
    return int(code)


def copy(directory, destination):
    transfer = directory / "output"
    if original_exit(directory, "analysis"):
        raise ValueError("Bazel analysis failed")
    if original_exit(directory, "build"):
        raise ValueError("original Bazel build failed")
    with regular(transfer / "elf.sha256", 65) as stream:
        elf = stream.read()
    if re.fullmatch(rb"[0-9a-f]{64}\n", elf) is None:
        raise ValueError("invalid test digest")
    with regular(transfer / "capsule.tar", 1 << 30) as stream:
        names = set()
        total = 0
        with tarfile.open(fileobj=stream, mode="r:") as archive:
            for entry in archive:
                path = PurePosixPath(entry.name)
                if (not path.parts or path.parts[0] not in ("container_test", "container_test.runfiles")
                        or (path.parts[0] == "container_test" and entry.name != "container_test")
                        or ".." in path.parts or entry.name in names or not (entry.isfile() or entry.isdir())):
                    raise ValueError("invalid runfile")
                names.add(entry.name)
                total += entry.size
                if total > 1 << 30 or len(names) > 32768:
                    raise ValueError("oversized runfiles")
                if entry.name == "container_test":
                    if not entry.isfile() or not entry.mode & 0o111:
                        raise ValueError("invalid executable")
                    with archive.extractfile(entry) as binary:
                        digest = hashlib.sha256()
                        for chunk in iter(lambda: binary.read(1024 * 1024), b""):
                            digest.update(chunk)
                    if digest.hexdigest().encode() != elf.strip():
                        raise ValueError("mismatched test executable")
        if not {"container_test", "container_test.runfiles/_main"} <= names:
            raise ValueError("missing executable or runfiles")
        stream.seek(0)
        with destination.open("wb") as out:
            shutil.copyfileobj(stream, out)
    (directory / "elf.sha256").write_bytes(elf)


def diagnostics(directory):
    schema = (directory / "failure-details.proto").read_text()
    enums = set(re.findall(r"^\s*([A-Z][A-Z_0-9]+)\s*=\s*\d+", schema, re.M))
    categories = {m[0].lower() + m[1:] for m in re.findall(r"\bmessage ([A-Z]\w*)", schema)}
    public = set((directory / "source-files").read_bytes().decode().split("\0"))
    report = {}
    for phase in ("analysis", "build"):
        path = directory / "output" / (phase + ".bep")
        try:
            path.lstat()
        except FileNotFoundError:
            continue
        row = report[phase] = {"codes": [], "source_locations": [], "fetch": [], "classes": []}
        with regular(path, 256 << 20) as stream:
            while raw := stream.readline((8 << 20) + 1):
                if len(raw) > 8 << 20:
                    raise ValueError("oversized build event")
                event = json.loads(raw)
                messages = [event.get("progress", {}).get("stderr", "")]
                finished = event.get("finished", {}).get("exitCode", {}).get("code")
                if type(finished) is int and 0 <= finished <= 255:
                    row["bep_exit"] = finished
                for part in (event.get("finished", {}), event.get("action", {}), event.get("aborted", {})):
                    detail = part.get("failureDetail", {})
                    messages.extend((part.get("description", ""), detail.get("message", "")))
                    for key, value in detail.items():
                        if key in categories and isinstance(value, dict) and value.get("code") in enums:
                            item = [key, value["code"]]
                            if item not in row["codes"] and len(row["codes"]) < 8:
                                row["codes"].append(item)
                progress = "\n".join(messages)
                if not progress:
                    continue
                for line in progress.splitlines():
                    if len(line) > 8192:
                        line = line[:4096] + " " + line[-4096:]
                    for filename, n in re.findall(r"(?<![a-zA-Z0-9_./+-])([a-zA-Z0-9_./+-]+):([0-9]{1,7})(?::|\b)", line):
                        path = next((k for k in (filename, filename.split("/src/", 1)[-1], filename.removeprefix("./")) if k in public), None)
                        if path and [path, int(n)] not in row["source_locations"] and len(row["source_locations"]) < 8:
                            row["source_locations"].append([path, int(n)])
                    for key, pattern in {
                        "fetch": r"download|fetch|UnknownHostException", "compile": r"compilepkg:|GoCompilePkg|compilation terminated",
                        "strict_deps": "missing strict dependencies", "undefined": r"undefined:|undefined reference",
                        "permission": "permission denied|operation not permitted", "device_space": "no space left on device",
                        "killed": r"signal: killed|signal 9|out of memory", "not_found": "No such file or directory|command not found",
                        "toolchain": "no matching toolchains|does not have mandatory providers", "opt": "unrecognized option|unrecognized command",
                        "stamp": "workspace status|workspace_status", "sandbox": "sandbox.*failed",
                    }.items():
                        if re.search(pattern, line, re.I) and key not in row["classes"]:
                            row["classes"].append(key)
                    if "musl-1.2.6.tar.gz" in line and re.search("error|exception|fail|timeout|could not", line, re.I):
                        row["fetch"] = ["musl-1.2.6.tar.gz"]
    (directory / "diagnostics.json").write_text(json.dumps(report))

def compiler_console(path, destination):
    # This is stdout/stderr of the actual stopped compiler, outside all its
    # writable mounts. Keep native phases and public phrases, never raw text.
    phrases = {
        "missing target": r"no such target|target .+ not declared in package",
        "server terminated abruptly": r"server terminated abruptly|unexpected EOF from server",
        "connection closed": r"socket closed|connection (?:closed|reset)|connection terminated",
        "disk full": r"no space left on device|disk quota exceeded",
        "out of memory": r"OutOfMemoryError|out of memory|cannot allocate memory",
        "process killed": r"signal: killed|killed by signal|out of memory|(?:^|: )killed\b",
        "permission denied": r"permission denied|operation not permitted",
        "internal error": r"internal error|internal bazel crash|internal bug",
        "source download failed": r"error downloading|failed to fetch|repository.*fetch.*fail",
        "no such file": r"no such file or directory|command not found",
        "compilation failed": r"compilepkg:|compilation terminated|undefined reference",
        "invalid option": r"unrecognized option|unrecognized command|unknown startup option",
        "sandbox failed": r"sandbox.*(?:failed|error)",
    }
    diagnostics, phases, resources, offset = [], [], [], 0
    with path.open("rb") as stream:
        # Capture the origin and the tail of large logs; never retain raw bytes.
        length = path.stat().st_size
        available = [(0, min(length, 262144))]
        if length > 262144:
            start = max(262144, length - 262144)
            available.append((start, length - start))
        for offset, size in available:
            stream.seek(offset)
            for line in stream.read(size).decode(errors="replace").splitlines():
                line = line[:8192]
                if m := re.fullmatch(r"QUAL-PHASE (probe|stock|fixed|tool) (source|analysis|build|locate|archive) (start|[0-9]{1,3})", line):
                    phases.append([m[1], m[2], m[3]])
                if m := re.fullmatch(r"QUAL-RESOURCE (free_mib|oom|oom_kill) ([0-9]{1,12})", line):
                    resources.append([m[1], int(m[2])])
                found = [key for key, pattern in phrases.items() if re.search(pattern, line, re.I)]
                if found and found not in diagnostics and len(diagnostics) < 16:
                    diagnostics.append(found)
    destination.write_text(json.dumps({"phases": phases[-64:], "diagnostics": diagnostics,
        "resources":resources[-48:], "console_bytes": length, "inspected_bytes":sum(sz for _, sz in available)}))


def observe(directory):
    # Status is independent of whether a capsule exists, opens or validates.
    for phase in ("analysis", "build", "locate", "archive"):
        try:
            original_exit(directory, phase)
        except FileNotFoundError:
            continue


if __name__ == "__main__":
    try:
        if sys.argv[1] == "observe":
            observe(Path(sys.argv[2]))
        elif sys.argv[1] == "console":
            compiler_console(Path(sys.argv[2]), Path(sys.argv[3]))
        elif sys.argv[1] == "diagnostics":
            diagnostics(Path(sys.argv[2]))
        else:
            copy(Path(sys.argv[1]), Path(sys.argv[2]))
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError) as error:
        sys.exit(type(error).__name__)
