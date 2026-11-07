"""Check release outputs against the original Git-free LLVM build."""
import collections
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

from release_tools import LABEL, NETGOFER, SOURCE, SOURCE_TREE, verify_files


REFERENCE = {
    "amd64": "abad05adf1f843b2ae4b4d65883419e10be214684045f7569f51b2cd1d24377b",
    "arm64": "9bbdd5ea576a63ab27f246d0e2c76e1a767f3db35b1c1e0b0afaae1d779f1975",
}
QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')


def arg_shape(value: str) -> str:
    # The measured container has only public build inputs. Keep paths useful
    # across different output bases and bound each field before uploading.
    value = re.sub(r"/(?:root/\.cache/bazel|workspace/bazel-[^/ ]+)/[^ ]*?/(external|execroot|bazel-out)/", r"\1/", value)
    return value[:140]


def arguments(trace: Path) -> dict:
    shapes: collections.Counter[tuple[str, ...]] = collections.Counter()
    with trace.open(errors="replace") as lines:
        for line in lines:
            if not re.search(r'execve(?:at)?\((?:[^,]+, )?"/(?:usr/)?bin/grep", \[', line):
                continue
            argv = line.split(", [", 1)[1].split("], ", 1)[0]
            args = QUOTED.findall(argv)
            if args:
                shapes[tuple(arg_shape(a) for a in args[1:8])] += 1
    return {"calls_with_argv": shapes.total(), "unique_shapes": len(shapes),
            "frequent": [{"arguments": list(s), "count": count} for s, count in shapes.most_common(14)]}


def inspect_arm(out: Path, mode: str, arch: str) -> dict:
    row = {"build_exit": int((out / "build.exit").read_text())}
    if (trace := out / "grep.trace").is_file():
        row["host_grep"] = arguments(trace)
    errors: list[str] = []
    with (out / "build.log").open(errors="replace") as lines:
        for line in lines:
            if "grep" in line and re.search(r"not found|no such file|cannot find|missing", line, re.I):
                detail = arg_shape(line.strip())
                if detail not in errors and len(errors) < 12:
                    errors.append(detail)
    row["missing_grep_diagnostics"] = errors
    finished = []
    targets = []
    if not (out / "build.bep").is_file():
        assert row["build_exit"] != 0
        row["bep_missing"] = True
        return row
    with (out / "build.bep").open() as stream:
        for line in stream:
            event = json.loads(line)
            if "finished" in event:
                finished.append(event["finished"].get("exitCode", {}).get("code", 0))
            if event.get("id", {}).get("targetCompleted", {}).get("label") == "//:release":
                if "completed" in event:
                    targets.append(event["completed"].get("success", False))
            if "aborted" in event and len(row.setdefault("aborted", [])) < 4:
                row["aborted"].append(arg_shape(str(event["aborted"].get("description", ""))))
            if event.get("action",{}).get("exitCode") and len(row.setdefault("failed_actions",[])) < 4:
                owner = event.get("id", {}).get("actionCompleted", {})
                row["failed_actions"].append([owner.get("label"),arg_shape(str(owner.get("primaryOutput",""))),event["action"].get("type")])
    assert finished == [row["build_exit"]], (mode, arch, finished, row["build_exit"])
    row["release_completed"] = targets
    if not row["build_exit"]:
        assert targets == [True], (mode,arch,targets)
        files = verify_files(out / "files", arch)["files"]
        manifest = "".join(f"{h}  {name}\n" for name,h in sorted(files.items()))
        row["files"] = files
        row["matches_original"] = hashlib.sha256(manifest.encode()).hexdigest() == REFERENCE[arch]
        if arch == "amd64":
            row["versions"] = [subprocess.check_output([out/"files"/n,"--version"],text=True,timeout=30).splitlines()[0]
                               for n in ("runsc",NETGOFER)]
            assert row["versions"] == ["runsc version " + LABEL] * 2
    return row


def main(directory: Path) -> None:
    report = {"source": SOURCE, "tree": SOURCE_TREE, "baseline_artifact": 13571015047, "arms": {}}
    output = directory / "grep-control.json"
    try:
        assert (directory/"source.tree").read_text().strip() == SOURCE_TREE
        for mode in ("absent","original"):
            assert (directory/mode/"mode").read_text().strip() == mode
            for arch in REFERENCE:
                report["arms"][mode+"-"+arch] = inspect_arm(directory/mode/arch,mode,arch)
        for arch in REFERENCE:
            original = report["arms"]["original-"+arch]
            assert original["build_exit"] == 0 and original["matches_original"] and original["host_grep"]["calls_with_argv"] > 0
        report["absent_matches_original"] = all(
            report["arms"]["absent-"+a].get("files") == report["arms"]["original-"+a]["files"] for a in REFERENCE)
        for arch in REFERENCE:
            absent = report["arms"]["absent-"+arch]
            if absent["build_exit"]:
                absent["outcome"] = "failed_with_missing_grep_diagnostic" if absent["missing_grep_diagnostics"] else "failed_without_missing_grep_diagnostic"
            else:
                absent["outcome"] = "matched_original_release" if absent["matches_original"] else "release_bytes_differ"
        report["original_controls_verified"] = True
    finally:
        payload=json.dumps(report, indent=2, sort_keys=True)
        assert len(payload) < 75000
        output.write_text(payload + "\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
