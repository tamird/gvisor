"""Verify the release on the outer runner; the producer does not need Python."""

from bisect import bisect_left
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

SOURCE = "facf1d8a895073c09d891f5bb890ab8274b8b020"
SOURCE_TREE = "b3816ea77c68df0db8de46ded13a35aa928155d9"
LABEL = "release-20261106.0"
# The original optimized registry-LLVM build used the same client and label.
CONTROL = "https://github.com/tamird/gvisor/actions/runs/42361840401/artifacts/13571015047"
LLVM_SOURCE = "https://bcr.bazel.build/modules/llvm/0.8.24/source.json"
MANIFESTS = {
    "amd64": (62, "abad05adf1f843b2ae4b4d65883419e10be214684045f7569f51b2cd1d24377b"),
    "arm64": (183, "9bbdd5ea576a63ab27f246d0e2c76e1a767f3db35b1c1e0b0afaae1d779f1975"),
}
ORIGINAL_OWNER = "//runsc/gofer/net/filter:filter_precompiled_gen"
GENERATORS = {
    ORIGINAL_OWNER, "//runsc/boot/filter:filter_precompiled_gen", "//runsc/gofer/fs/filter:filter_precompiled_gen",
    "//pkg/sentry/platform/kvm:mmapfilter_precompiled_gen", "//pkg/sentry/platform/ptrace:stubfilter_precompiled_gen",
    "//pkg/sentry/platform/systrap:stubfilter_precompiled_gen",
}
# Each of 123 template lines reaches the first predicate. Exactly one line
# matches each predicate, leaving 122 and 121 invocations of the next two.
EXPECTED_GREPS = {
    "REPLACED_IMPORT_THIS_IS_A_LOAD_BEARING_COMMENT": 123,
    "PROGRAMS_FUNC_THIS_IS_A_LOAD_BEARING_COMMENT": 122,
    "go:embed precompiled_lib.tmpl.go": 121,
}
NETGOFER = "gvisor-bin/runsc-netgofer"
FILES = {
    "runsc", "containerd-shim-runsc-v1", NETGOFER, "gvisor-bin/checkpointgofer",
    "gvisor-bin/gvisor-proc-umounter", "gvisor-bin/gvisor-sentry-prewarmer",
    "gvisor-bin/gvisor_sentry", "gvisor-bin/runsc-fd-parking", "gvisor-bin/runsc-metric-server",
}
EXEC = re.compile(r'\bexecve(?:at)?\((?:[^,"]+, )?"([^"]+)"')
FORK = re.compile(r"\b(?:clone3?|v?fork)(?:\(| resumed>).*?= ([1-9]\d*)\s*$")
SYSTEM = re.compile(r"/(?:usr/(?:local/)?)?s?bin/[A-Za-z0-9._+-]+")
REPOSITORY = re.compile(r"(?:^|/)external/([A-Za-z0-9._+~-]{1,100})/")
ERROR = re.compile(r"error|failed|not found|no such file|not permitted|permission denied|cannot", re.I)


def diagnostics(path: Path) -> list[str]:
    if not path.is_file():
        return []
    lines: list[str] = []
    with path.open(errors="replace") as stream:
        for line in stream:
            if ERROR.search(line) and len(lines) < 14:
                lines.append(line.strip()[:450])
    return lines


def generation_actions(path: Path) -> dict:
    owners = defaultdict(Counter)
    if path.is_file():
        with path.open() as stream:
            for line in stream:
                if '"actionCompleted"' not in line:
                    continue
                event = json.loads(line)
                action = event.get("action", {})
                owner = event["id"].get("actionCompleted", {}).get("label", "").lstrip("@")
                kind = action.get("type")
                if owner in GENERATORS and action.get("success") and kind in {"Genrule", "TemplateExpand"}:
                    owners[owner][kind] += 1
    return dict(owners)


def original_action(directory: Path) -> dict:
    row = {"owner": ORIGINAL_OWNER, "qualified": False, "grepExecs": 0, "markerExecs": dict.fromkeys(EXPECTED_GREPS, 0)}
    exit_file = directory / "build.exit"
    if not exit_file.is_file():
        return row
    row["exit"] = int(exit_file.read_text())
    if row["exit"]:
        row["diagnostics"] = diagnostics(directory / "build.log")
        return row
    row["actions"] = generation_actions(directory / "events.jsonl")
    for trace_file in directory.glob("grep.trace.*"):
        with trace_file.open(errors="replace") as stream:
            for line in stream:
                match = EXEC.search(line)
                if not match or match[1] not in {"/bin/grep", "/usr/bin/grep"} or not line.rstrip().endswith("= 0"):
                    continue
                row["grepExecs"] += 1
                for marker in EXPECTED_GREPS:
                    if f'"-q", "{marker}"' in line:
                        row["markerExecs"][marker] += 1
    row["qualified"] = row["actions"] == {ORIGINAL_OWNER: {"Genrule": 1}} and row["markerExecs"] == EXPECTED_GREPS
    return row


def executions(path: Path) -> dict:
    host: Counter[str] = Counter()
    other: Counter[str] = Counter()
    unparsed = 0
    thread_replacements = 0
    pending: dict[str, str] = {}
    parents = {}
    history = defaultdict(list)
    system_calls = []
    with path.open(errors="replace") as stream:
        for index, line in enumerate(stream):
            if child := FORK.search(line):
                parents[child[1]] = (line.split(maxsplit=1)[0], index)
                continue
            if "execve" not in line:
                continue
            if "+++ superseded by execve in pid" in line:
                thread_replacements += 1
                continue
            pid = line.split(maxsplit=1)[0]
            if match := EXEC.search(line):
                executable = match[1]
                if "<unfinished ...>" in line:
                    pending[pid] = executable
                    continue
            elif "<... execve" in line and pid in pending:
                executable = pending.pop(pid)
            else:
                unparsed += 1
                continue
            if not line.rstrip().endswith("= 0"):
                unparsed += 1
                continue
            if SYSTEM.fullmatch(executable):
                host[executable] += 1
                if Path(executable).name in {"grep", "bash", "env"}:
                    system_calls.append((index, pid, executable))
                origin = executable
            elif repo := REPOSITORY.search(executable):
                other["external repository executable"] += 1
                origin = "external/" + repo[1]
            elif executable.endswith("tools/workspace_status.sh"):
                other["workspace status script"] += 1
                origin = "workspace status script"
            elif "/bazel-out/" in executable or executable.startswith("bazel-out/"):
                other["Bazel build output"] += 1
                origin = "Bazel build output"
            else:
                other["other executable"] += 1
                origin = "other executable"
            # Preserve the parent binary's basename, without its argument
            # vector or any arbitrary workspace/runner path components.
            if origin in {"Bazel build output", "other executable"}:
                name = Path(executable).name
                if re.fullmatch(r"[A-Za-z0-9._+-]{1,64}", name):
                    origin += "/" + name
            history[pid].append((index, origin))
    # Select the nearest observed executable before the helper started. A fork
    # can return in the parent after its child has already executed.
    origins = defaultdict(Counter)
    repositories = defaultdict(Counter)
    for index, pid, executable in system_calls:
        origin = None
        repository = None
        seen = set()
        while pid and pid not in seen:
            seen.add(pid)
            events = history.get(pid, [])
            end = bisect_left(events, (index,))
            if end:
                origin = origin or events[end - 1][1]
                for pos in range(end - 1, -1, -1):
                    name = events[pos][1]
                    if name.startswith("external/"):
                        repository = name
                        break
            if repository or pid not in parents:
                break
            pid, fork = parents[pid]
            index = min(index, fork)
        origins[executable][origin or "unresolved"] += 1
        repositories[executable][repository or "unresolved"] += 1
    return {"systemExecutables": dict(host), "otherExecutions": dict(other),
            "observedOrigins": {exe: counts.most_common(12) for exe, counts in origins.items()},
            "observedRepositoryAncestors": {exe: counts.most_common(12) for exe, counts in repositories.items()},
            "threadReplacements": thread_replacements, "unparsed": unparsed + len(pending)}


def verify_files(directory: Path, arch: str) -> dict:
    machine, expected = MANIFESTS[arch]
    files: dict[str, str] = {}
    for file in sorted(directory.rglob("*")):
        if file.is_dir():
            continue
        assert file.is_file() and not file.is_symlink() and os.access(file, os.X_OK), file.name
        assert 64 <= file.stat().st_size < 160 << 20, file.name
        with file.open("rb") as stream:
            header = stream.read(64)
            assert header[:6] == b"\x7fELF\x02\x01" and int.from_bytes(header[18:20], "little") == machine, file.name
            stream.seek(0)
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(1 << 20), b""):
                digest.update(block)
        files[file.relative_to(directory).as_posix()] = digest.hexdigest()
    assert files.keys() == FILES, sorted(files)
    manifest = "".join(f"{digest}  {name}\n" for name, digest in sorted(files.items()))
    actual = hashlib.sha256(manifest.encode()).hexdigest()
    return {"files": files, "machine": machine, "manifest": actual, "matchesOriginal": actual == expected}


def main(directory: Path) -> bool:
    receipt = {"source": SOURCE, "image": "sha256:8a2321a92b438238f93b4346fff61bc4259c99fdd4d9b52ee86f153fee0c344f",
               "control": CONTROL, "releaseLabel": LABEL, "compilationMode": "opt",
               "llvmModule": "llvm@0.8.24", "llvmRegistrySource": LLVM_SOURCE,
               "verified": False, "targets": {}}
    try:
        receipt["tree"] = (directory / "source.tree").read_text().strip()
        assert receipt["tree"] == SOURCE_TREE
        source = directory.parent / "exported-source"
        receipt["moduleSHA256"] = hashlib.sha256((source / "MODULE.bazel").read_bytes()).hexdigest()
        patch = source / "tools/llvm-linux-header-default.patch"
        receipt["llvmHeaderPatchSHA256"] = hashlib.sha256(patch.read_bytes()).hexdigest()
        assert receipt["moduleSHA256"] == "484a819e72dd3f941ccce750304d272dafaa7fae10d6adee24fdb7ff22a9b020"
        assert receipt["llvmHeaderPatchSHA256"] == "4e5a1ff71b05f0105e3aeefc7383da5d903343f37d2046e386c67b063f1cf7e8"
        lock = source / "MODULE.bazel.lock"
        if lock.is_file():
            receipt["resolvedLLVMRegistrySourceSHA256"] = json.loads(lock.read_text()).get("registryFileHashes", {}).get(LLVM_SOURCE)
        if (tool_path := directory / "client-tools").exists():
            receipt["clientTools"] = dict(line.split("\t") for line in tool_path.read_text().splitlines())
        if (marker := directory / "git-metadata").exists():
            receipt["gitMetadata"] = marker.read_text().strip()
        if (client := directory / "client-version").exists():
            receipt["bazelVersion"] = client.read_text().strip()
        if (roots := directory / "public-roots.pem").exists():
            receipt["httpsRootsSHA256"] = hashlib.sha256(roots.read_bytes()).hexdigest()
        receipt["traceProbeDiagnostics"] = diagnostics(directory / "probe.error")
        try:
            receipt["originalAction"] = original_action(directory / "original-action")
        except (OSError, ValueError) as error:
            receipt["originalAction"] = {"qualified": False, "observerError": type(error).__name__}
        for arch in MANIFESTS:
            target = directory / arch
            row = receipt["targets"][arch] = {}
            if (code := target / "build.exit").exists():
                row["buildExit"] = int(code.read_text())
            if (trace := target / "exec.trace").exists():
                row["exec"] = executions(trace)
            try:
                row["generationActions"] = generation_actions(target / "events.jsonl")
            except (OSError, ValueError) as error:
                row["generationActions"] = {}
                row["eventsError"] = type(error).__name__
            if row.get("buildExit") != 0:
                row["diagnostics"] = diagnostics(target / "build.log")
                continue
            try:
                row.update(verify_files(target / "files", arch))
                if arch == "amd64":
                    row["versions"] = {}
                    for name in ("runsc", NETGOFER):
                        output = subprocess.check_output([target / "files" / name, "--version"], text=True, timeout=30)
                        row["versions"][name] = output.splitlines()[0]
            except (AssertionError, IndexError, OSError, subprocess.SubprocessError) as error:
                row["verificationError"] = str(error)[:400]
        # The first build starts from a cold client. Allow Bazel to reuse
        # actions for the second target when their inputs and outputs match.
        observed = defaultdict(set)
        for row in receipt["targets"].values():
            for owner, kinds in row["generationActions"].items():
                observed[owner].update(kinds)
        receipt["templateActionsVerified"] = observed.keys() == GENERATORS and all(kinds == {"TemplateExpand"} for kinds in observed.values())
        receipt["verified"] = (
            receipt.get("gitMetadata") == "absent" and receipt.get("bazelVersion") == "bazel 9.3.1"
            and receipt["originalAction"]["qualified"] and receipt["templateActionsVerified"]
            and all(t.get("matchesOriginal") and "exec" in t and "eventsError" not in t for t in receipt["targets"].values())
            and receipt["targets"]["amd64"].get("versions") == dict.fromkeys(("runsc", NETGOFER), "runsc version " + LABEL)
            and all(not receipt["clientTools"][t] for t in ("git", "python3", "patch", "zip", "unzip", "cc", "gcc", "clang", "as", "ld", "ar", "make", "docker"))
        )
        return receipt["verified"]
    finally:
        raw = json.dumps(receipt, sort_keys=True, indent=2)
        assert len(raw) < 65536
        (directory / "release-tools.json").write_text(raw + "\n")


if __name__ == "__main__":
    sys.exit(0 if main(Path(sys.argv[1])) else 1)
