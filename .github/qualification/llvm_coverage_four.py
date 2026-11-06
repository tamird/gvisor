"""Bind four public Bazel target results to the coverage executables they produced."""
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
from urllib.parse import unquote, urlparse


WORK = Path(os.environ["RUNNER_TEMP"]) / "llvm-coverage-four"
TARGETS = {
    "//runsc:runsc_coverage": "runsc_cov",
    "//runsc:runsc_race_coverage": "runsc_race_cov",
    "//runsc/cmd/sentry:gvisor_sentry_coverage": "gvisor_sentry_cov",
    "//runsc/cmd/sentry:gvisor_sentry_race_coverage": "gvisor_sentry_race_cov",
}
SAFE_LABEL = re.compile(r"(?:@@?[\w+.~/\-]+)?//[\w/+.@:\-]+")
CAS = re.compile(r"/(?:blobs|compressed-blobs/zstd)/([a-f0-9]{64})/(\d+)$")


def default_files(sets, references, name):
    pending = list(references)
    visited = set()
    files = {}
    while pending:
        ref = pending.pop()
        if ref in visited:
            continue
        visited.add(ref)
        item = sets.get(ref, {})
        pending.extend(entry["id"] for entry in item.get("fileSets", []))
        for item_file in item.get("files", []):
            if Path(item_file.get("name", "")).name != name:
                continue
            uri = urlparse(item_file.get("uri", ""))
            if uri.scheme == "file" and uri.netloc in ("", "localhost"):
                path = Path(unquote(uri.path))
                if path.name == name and path.is_file():
                    files[path] = item_file
    return files


def inspect_elf(path):
    with path.open("rb") as binary:
        header = binary.read(64)
        if len(header) != 64 or header[:7] != b"\x7fELF\x02\x01\x01":
            return {"elf64_le": False}
        kind, machine = struct.unpack_from("<HH", header, 16)
        offset, = struct.unpack_from("<Q", header, 32)
        entry_size, count = struct.unpack_from("<HH", header, 54)
        size = path.stat().st_size
        if entry_size != 56 or not 0 < count < 1024 or offset + count * entry_size > size:
            return {"valid_program_header_table": False}
        binary.seek(offset)
        segments = [struct.unpack("<IIQQQQQQ", binary.read(entry_size)) for _ in range(count)]
        executable = any(t == 1 and flags & 1 and length > 0 and begin + length <= size
                         for t, flags, begin, _, _, length, _, _ in segments)
        binary.seek(0)
        digest = hashlib.sha256()
        for block in iter(lambda: binary.read(1024 * 1024), b""):
            digest.update(block)
    result = {"elf64_le": True, "type": kind, "machine": machine, "exec_load": executable,
              "mode": path.stat().st_mode & 0o777, "size": size, "sha256": digest.hexdigest()}
    # Retain symbol evidence separately from the target's successful Go link.
    nm = subprocess.Popen(["nm", "--defined-only", str(path)], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL)
    coverage = race = 0
    for line in nm.stdout:
        name = line.rsplit(None, 1)[-1].strip() if line.strip() else b""
        if b"goCover_" in name or b".GoCover_" in name:
            coverage += 1
        if name in (b"__tsan_init", b"runtime.raceinit"):
            race += 1
    result.update(nm_exit=nm.wait(), go_cover_symbols=coverage, race_runtime_symbols=race)
    return result


def run():
    report = {"source": os.environ["SOURCE"], "config": "source-presubmit",
              "targets": {label: {} for label in TARGETS}, "failed_actions": [], "verified": False}
    result = WORK / "result.json"

    def save():
        result.write_text(json.dumps(report, indent=2) + "\n")

    save()
    status = WORK / "build.exit"
    if status.is_file():
        report["bazel_exit"] = int(status.read_text())
    sets = {}
    references = {}
    events = WORK / "build.bep"
    if events.is_file():
        with events.open() as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                ids = event.get("id", {})
                if "started" in event and re.fullmatch(r"[\da-f-]{36}", event["started"].get("uuid", "")):
                    report["invocation"] = event["started"]["uuid"]
                if "namedSet" in ids:
                    sets[ids["namedSet"]["id"]] = event.get("namedSetOfFiles", {})
                target = ids.get("targetCompleted", {})
                label = target.get("label")
                if label in TARGETS and not target.get("aspect") and "completed" in event:
                    body = event["completed"]
                    report["targets"][label]["completed"] = body.get("success", False)
                    references[label] = [ref["id"] for group in body.get("outputGroup", [])
                                         if group["name"] == "default" for ref in group.get("fileSets", [])]
                action = event.get("action", {})
                if action and not action.get("success", False) and len(report["failed_actions"]) < 12:
                    identifier = ids.get("actionCompleted", {})
                    owner = identifier.get("label", "")
                    mnemonic = action.get("type", "")
                    if SAFE_LABEL.fullmatch(owner) and re.fullmatch(r"[\w.\-]{1,64}", mnemonic):
                        failed = {"owner": owner, "type": mnemonic, "exit": action.get("exitCode")}
                        cas = CAS.search(action.get("stderr", {}).get("uri", ""))
                        if cas:
                            failed.update(stderr_sha256=cas[1], stderr_size=int(cas[2]))
                        report["failed_actions"].append(failed)
    log = WORK / "build.log"
    if log.is_file():
        patterns = ("Resource Exhausted", "RESOURCE_EXHAUSTED", "Deadline Exceeded",
                    "OutOfMemoryError", "No space left on device", "undefined reference",
                    "missing strict dependencies", "curl: command not found", "Remote Execution")
        found = set()
        with log.open(errors="replace") as stream:
            for line in stream:
                found.update(p for p in patterns if p.lower() in line.lower())
        report["log_signals"] = sorted(found)
    save()
    for label, name in TARGETS.items():
        row = report["targets"][label]
        try:
            files = default_files(sets, references.get(label, []), name)
            row["local_default_output_count"] = len(files)
            if len(files) == 1:
                path, = files
                item = files[path]
                row["default_output"] = name
                row.update(inspect_elf(path))
                declared = item.get("digest")
                if declared and re.fullmatch(r"[a-f0-9]{64}", declared):
                    row["bep_sha256_matches"] = row.get("sha256") == declared
                if (label.startswith("//runsc:") and row.get("machine") == 62
                        and row.get("exec_load") and os.access(path, os.X_OK)):
                    check = subprocess.run([str(path), "--version"], capture_output=True, timeout=30)
                    row.update(native_cli_exit=check.returncode,
                               native_cli_version=check.stdout.startswith(b"runsc version "))
        except (OSError, ValueError, struct.error, subprocess.SubprocessError) as exc:
            row["measurement_error"] = type(exc).__name__
        save()
    rows = list(report["targets"].values())
    report["verified"] = bool(
        report.get("bazel_exit") == 0 and report.get("invocation")
        and all(row.get("completed") and row.get("machine") == 62 and row.get("exec_load")
                and row.get("type") in (2, 3) and row.get("mode", 0) & 0o111
                and row.get("bep_sha256_matches", True) for row in rows)
        and len({row.get("sha256") for row in rows}) == 4)
    save()


if __name__ == "__main__":
    run()
