"""Verify the release on the outer runner; the producer does not need Python."""

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

SOURCE = "c115840f8a85010111fa5d72b611d7d9ab1df34b"
# sha256sum-style manifests of the nine original files, ordered by relative
# name. Both come from the verified compiler-free producer on this source.
CONTROL = "https://github.com/tamird/gvisor/actions/runs/42356646010/artifacts/13568470786"
MANIFESTS = {
    "amd64": (62, "09594597be04e4e5d27f07b8d2911ec01f96a2617236e83129d3ef74ee94b8fd"),
    "arm64": (183, "e8419e47d4117fd8658e7f43d80353e385a221c5db82bc2d16671014b9d6d700"),
}
EXEC = re.compile(r'\bexecve(?:at)?\((?:[^,"]+, )?"([^"]+)"')
SYSTEM = re.compile(r"/(?:usr/(?:local/)?)?s?bin/[A-Za-z0-9._+-]+")
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


def executions(path: Path) -> dict:
    host: Counter[str] = Counter()
    other: Counter[str] = Counter()
    unparsed = 0
    with path.open(errors="replace") as stream:
        for line in stream:
            if "execve" not in line:
                continue
            match = EXEC.search(line)
            if not match or not line.rstrip().endswith("= 0"):
                unparsed += 1
                continue
            executable = match[1]
            if SYSTEM.fullmatch(executable):
                host[executable] += 1
            elif executable.endswith("tools/workspace_status.sh"):
                other["workspace status script"] += 1
            elif "/external/" in executable or executable.startswith("external/"):
                other["external repository executable"] += 1
            elif "/bazel-out/" in executable or executable.startswith("bazel-out/"):
                other["Bazel build output"] += 1
            else:
                other["other executable"] += 1
    return {"systemExecutables": dict(host), "otherExecutions": dict(other), "unparsed": unparsed}


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
    manifest = "".join(f"{digest}  {name}\n" for name, digest in sorted(files.items()))
    actual = hashlib.sha256(manifest.encode()).hexdigest()
    return {"files": files, "machine": machine, "manifest": actual, "matchesControl": len(files) == 9 and actual == expected}


def main(directory: Path) -> bool:
    receipt = {"source": SOURCE, "image": "sha256:8a2321a92b438238f93b4346fff61bc4259c99fdd4d9b52ee86f153fee0c344f",
               "control": CONTROL, "verified": False, "targets": {}}
    try:
        assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
        if (tool_path := directory / "client-tools").exists():
            receipt["clientTools"] = dict(line.split("\t") for line in tool_path.read_text().splitlines())
        if (stamp := directory / "stamp").exists():
            receipt["stamp"] = stamp.read_text().strip()
        if (code := directory / "git-describe.exit").exists():
            receipt["gitDescribeExit"] = int(code.read_text())
            with (directory / "git-describe.log").open(errors="replace") as stream:
                receipt["gitDescribeOutput"] = stream.read(1600)
        if (bound := directory / "git-binding.sha256").exists():
            receipt["boundGitSHA256"] = bound.read_text().split()[0]
        receipt["traceProbeDiagnostics"] = diagnostics(directory / "probe.error")
        for arch in MANIFESTS:
            target = directory / arch
            row = receipt["targets"][arch] = {}
            if (code := target / "build.exit").exists():
                row["buildExit"] = int(code.read_text())
            if (trace := target / "exec.trace").exists():
                row["exec"] = executions(trace)
            if row.get("buildExit") != 0:
                row["diagnostics"] = diagnostics(target / "build.log")
                continue
            try:
                row.update(verify_files(target / "files", arch))
                if arch == "amd64" and row["matchesControl"]:
                    output = subprocess.check_output([target / "files/runsc", "--version"], text=True)
                    row["version"] = output.partition("\n")[0]
            except (AssertionError, OSError, subprocess.CalledProcessError) as error:
                row["verificationError"] = str(error)[:400]
        receipt["verified"] = (
            receipt.get("stamp") == "STABLE_VERSION " + SOURCE[:12]
            and all(t.get("matchesControl") for t in receipt["targets"].values())
            and receipt["targets"]["amd64"].get("version") == "runsc version " + SOURCE[:12]
            and all(not receipt["clientTools"][t] for t in ("cc", "gcc", "clang", "as", "ld", "ar", "make", "docker"))
        )
        return receipt["verified"]
    finally:
        raw = json.dumps(receipt, sort_keys=True, indent=2)
        assert len(raw) < 65536
        (directory / "release-tools.json").write_text(raw + "\n")


if __name__ == "__main__":
    sys.exit(0 if main(Path(sys.argv[1])) else 1)
