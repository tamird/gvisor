"""Compare the actual 8.5.1 formatter, existing selector, and Bazel visibility."""
import ast
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import traceback

work = Path(os.environ["RUNNER_TEMP"])
source = "1f6f8dd9f3f2fad74a597cd034ba30523cf7193c"
tree = "e2b9c0ba1a447ab20560e9eb8740fe7d62aad5a6"
binary = "887377fc64d23a850f4d18a077b5db05b19913f4b99b270d193f3c7334b5a9a7"
missing = (
    "MODULE.bazel", "governance/tools/maintainers/repositories.bzl",
    "tools/bazeldefs/extensions/coral_crosstool.bzl", "tools/bazeldefs/extensions/crosstool.bzl",
    "tools/bazeldefs/extensions/llvm_zlib.bzl", "tools/cc_compile_inputs.bzl", "tools/show_paths.bzl",
)
record = {"source": source, "tree": tree, "status": "INCOMPLETE"}
def save():
    (work / "buildifier.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")

def output(*argv):
    return subprocess.check_output(argv, text=True, stderr=subprocess.PIPE)

def run(name, args):
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    text = p.stdout.decode(errors="replace")
    record[name] = {"exit": p.returncode, "sha256": sha256(p.stdout).hexdigest(), "bytes": len(p.stdout),
                    "invocations": sorted(set(re.findall(r"app\.buildbuddy\.io/invocation/([0-9a-f-]{36})", text))),
                    "hints": [x for x in ("not found", "Unable to locate", "unsupported", "not visible", "checksum", "ERROR:") if x in text]}
    save()
    return p.returncode, text

def main():
    assert output("git", "rev-parse", "HEAD").strip() == source
    assert output("git", "rev-parse", "HEAD^{tree}").strip() == tree
    files = output("git", "ls-files", "-z", "--", "BUILD", "*/BUILD", "*.bzl", "WORKSPACE", "MODULE.bazel").split("\0")[:-1]
    assert len(files) == len(set(files)) == 614 and set(missing) <= set(files)
    record["tracked"] = {"number": len(files), "sha256": sha256(("\0".join(files) + "\0").encode()).hexdigest()}
    assert shutil.which("strace")
    trace = work / "buildifier.trace"
    exit_code, _ = run("clean", ["strace", "-f", "-qq", "-s", "65535", "-e", "trace=execve",
                                "-o", str(trace), "tools/lint.sh", "buildifier"])
    if exit_code:
        raise ValueError("canonical clean formatter")
    actual, digests = [], set()
    for line in trace.read_text().splitlines():
        if "--mode=check" not in line or "execve(" not in line or not line.endswith("= 0"):
            continue
        m = re.search(r'execve\((".*?"), (\[.*?\]), ', line)
        if not m:
            continue
        exe = Path(ast.literal_eval(m[1]))
        if not exe.is_file() or not exe.is_absolute():
            continue
        digest = sha256(exe.read_bytes()).hexdigest()
        if digest != binary:
            continue
        args = ast.literal_eval(m[2])
        assert args[1:3] == ["--mode=check", "--lint=warn"]
        assert args[3] == "--warnings=-duplicated-name,-list-append,-native-py,-native-sh-binary,-native-sh-library,-native-sh-test,-native-proto"
        actual.extend(args[4:])
        digests.add(digest)
    record["physical_cli"] = {"files": len(actual), "sha256": sha256(("\0".join(actual) + "\0").encode()).hexdigest(),
                              "executables": sorted(digests)}
    contents = {name: Path(name).read_bytes() for name in missing}
    untracked = Path("tools/uncatalogued_buildifier_control.bzl")
    assert not untracked.exists()
    untracked.write_text("not a valid build file @@@\n")
    try:
        for name, original in contents.items():
            Path(name).write_bytes(original + b"\n\n")
        exit_code, text = run("seven_bad", ["tools/lint.sh", "buildifier"])
        record["diffs"] = [p for p in missing if ("--- " + p + "\n") in text and ("+++ " + p + " (formatted)\n") in text]
        assert exit_code and len(record["diffs"]) == 7
        assert run("fix", ["tools/lint.sh", "--fix", "buildifier"])[0] == 0
        assert all(Path(name).read_bytes() == original for name, original in contents.items())
        assert not output("git", "diff", "--name-only")
        record["untracked_unchanged"] = untracked.read_text() == "not a valid build file @@@\n"
    finally:
        untracked.unlink(missing_ok=True)
        for name, original in contents.items():
            Path(name).write_bytes(original)
    vis = output("bazel", "query", "--output=label", 'visible(//:license, (kind("source file", //...:*) union buildfiles(//...:*)))')
    candidates = set()
    for lab in vis.splitlines():
        if lab.startswith("//"):
            pack, _, target = lab[2:].partition(":"); candidates.add((pack + "/" + target).lstrip("/"))
    record["central_visibility"] = {"root": "//:license", "available": len(candidates.intersection(files)),
                                   "builds": sum(Path(f).name == "BUILD" for f in candidates.intersection(files))}
    ex, text = run("license", ["bazel", "run", "--run_in_cwd", "//tools/licensecheck/main:licensecheck", "--", "--mode=verify"])
    record["license"]["count"] = [int(n) for n in re.findall(r"verified ([0-9]+) dependencies", text, re.I)]
    assert ex == 0 and record["license"]["count"] == [221]
    assert actual == files and digests == {binary} and record["untracked_unchanged"]
    record["status"] = "PASS"

try:
    main()
except Exception as exc:
    record["exception"] = type(exc).__name__
    record["lines"] = [x.lineno for x in traceback.extract_tb(exc.__traceback__) if Path(x.filename).name == "buildifier.py"][-3:]
finally:
    save()
raise SystemExit(int(record["status"] != "PASS"))
