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
source = "5fef0e7301bf61aa5043c8014222c1b8f3b6ca77"
tree = "dc44cd1ac936ef5ea93f5a28ba652e202b2dbe20"
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
    record["stage"] = name
    save()
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=540)
    text = p.stdout.decode(errors="replace")
    record[name] = {"exit": p.returncode, "sha256": sha256(p.stdout).hexdigest(), "bytes": len(p.stdout),
                    "invocations": sorted(set(re.findall(r"app\.buildbuddy\.io/invocation/([0-9a-f-]{36})", text))),
                    "hints": [x for x in ("not found", "Unable to locate", "unsupported", "not visible", "checksum", "ERROR:") if x in text]}
    save()
    return p.returncode, text

def main():
    assert output("git", "rev-parse", "HEAD").strip() == source
    assert output("git", "rev-parse", "HEAD^{tree}").strip() == tree
    for name, args in (
        ("registered formatter", ["tools/lint.sh", "buildifier"]),
        ("C++ formatter", ["tools/lint.sh", "cpplint"]),
        ("driver tests", ["bazel", "test", "--test_output=all", "//tools:lint_test"]),
        ("merged licenses", ["bazel", "run", "--run_in_cwd", "//tools/licensecheck/main:licensecheck", "--", "--mode=verify"]),
    ):
        code, text = run(name, args)
        if name == "driver tests":
            assert "Ran 4 tests" in text and "OK" in text
        if name == "merged licenses":
            matches = re.findall(r"all ([0-9]+) dependencies have up-to-date license entries", text, re.I)
            assert len(matches) == 1
            record["license_count"] = int(matches[0])
        if code:
            raise RuntimeError(f"{name}: exit {code}")
    record["status"] = "PASS"

try:
    main()
except Exception as exc:
    record["exception"] = type(exc).__name__
    record["lines"] = [x.lineno for x in traceback.extract_tb(exc.__traceback__) if Path(x.filename).name == "buildifier.py"][-3:]
finally:
    save()
raise SystemExit(int(record["status"] != "PASS"))
