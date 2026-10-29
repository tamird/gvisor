import hashlib
import json
import os
import platform
import subprocess
from pathlib import Path

scratch = Path(os.environ["RUNNER_TEMP"])
report = {"verdict": "INCOMPLETE", "base": "c08c9afd96151d3794590beb986d037d10b847a3",
          "tree": "f80ffe624e569939e369281b03f6c1c85f20fbfb", "machine": platform.machine()}


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def check():
    assert run("git", "rev-parse", "HEAD") == report["base"]
    run("git", "apply", "--index", "../carrier/.github/qualification/lint-native-label/upstream.patch")
    assert run("git", "write-tree") == report["tree"]
    assert report["machine"] == "aarch64"
    run("bazel", "build", "//buildifier:native")
    output, = run("bazel", "cquery", "//buildifier:native", "--output=files").splitlines()
    binary = Path(run("bazel", "info", "execution_root")) / output
    report["native_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
    assert report["native_sha256"] == "38d2ed845f560b4a16ddee41de906508a95f8dc85b04e0851b0a71e3a70d3890"
    report["version"] = run(binary, "-version")
    assert "10.1.0" in report["version"]
    run(binary, "-mode=check", "buildifier/BUILD", "buildifier/buildifier_binary.bzl")
    targets = {"//tests/binary_runner:binary_runner_test", "//tests/buildifier:buildifier_test"}
    bep = scratch / "upstream-test-events.json"
    run("bazel", "test", "--nocache_test_results", "--build_event_json_file=" + str(bep), *sorted(targets))
    events = [json.loads(s) for s in bep.read_text().splitlines()]
    report["tests"] = {r["id"]["testSummary"]["label"]: r["testSummary"]["overallStatus"]
                       for r in events if "testSummary" in r and "testSummary" in r["id"]}
    assert report["tests"] == dict.fromkeys(targets, "PASSED")
    report["verdict"] = "PASS"


try:
    check()
finally:
    (scratch / "upstream-label.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, sort_keys=True))
