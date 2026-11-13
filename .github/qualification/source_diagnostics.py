"""Preserve focused diagnostics from the native presubmit on the unmodified source."""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(os.environ["RUNNER_TEMP"]) / "llvm-source-presubmit"
SOURCE = "48f457b4e040312422b8e28b97081998d1d81297"
TESTS = (
    "//:github_workflows_test",
    "//examples/sandboxexec/go/docprocessor:docprocessor_test",
    "//pkg/sentry/devices/nvproxy:nvproxy_driver_parity_test",
    "//runsc/sandbox:sandbox_test",
    "//sandboxexec/sandbox/python:sandbox_py_test",
    "//sandboxexec/sandbox:sandbox_test",
    "//test/trace:trace_test",
    "//vdso:vdso_test",
)
COVERAGE = (
    "//runsc:runsc_coverage",
    "//runsc:runsc_race_coverage",
    "//runsc/cmd/sentry:gvisor_sentry_coverage",
    "//runsc/cmd/sentry:gvisor_sentry_race_coverage",
)
# The remote jobs have no ambient credentials. Also exclude connection metadata
# when selecting excerpts from Bazel's own client log.
PRIVATE = re.compile(r"authorization|bearer |api[-_]key|remote_header|bes_header|github_pat_|ghp_|BEGIN .*PRIVATE KEY", re.I)
ERROR = re.compile(r"error|fail|panic|fatal|missing|not found|no such|denied|killed|readelf|unshare|namespace|clone|not permitted|refused|unavailable|could not|unable to|unsupported|not installed|expected|got |assert|traceback|raised|timed out|/bin/(?:ba)?sh|curl:|tar:", re.I)


def excerpt(lines: list[str]) -> list[str]:
    """Keep the first error contexts and final lines of a potentially large syscall/test log."""
    safe = [s.rstrip() for s in lines if not PRIVATE.search(s)]
    hits = [i for i, s in enumerate(safe) if ERROR.search(s) and not s.startswith("[")]
    indices = set(range(min(4, len(safe)))) | set(range(max(len(safe)-8, 0), len(safe)))
    for i in hits[:70]:
        indices.update(range(max(i-1, 0), min(i+3, len(safe))))
    return [safe[i][:1000] for i in sorted(indices)]


def record(arch: str, command: str, labels: tuple[str, ...]) -> bool:
    cpu = {"amd64": "x86_64", "arm64": "aarch64"}[arch]
    platform = "//tools/bazeldefs:linux_" + arch
    flags = [
        str(ROOT / "bazel"), "--host_jvm_args=-Xmx10g", "--nosystem_rc", "--nohome_rc",
        "--bazelrc=" + str(ROOT / "remote.rc"), command, "--config=" + cpu,
        "--host_platform=" + platform, "--extra_execution_platforms=" + platform,
        "--remote_default_exec_properties=Arch=" + arch, "--distdir=" + str(ROOT / "dist"),
        "--keep_going", "--jobs=64", "--strip=never", "--test_env=CGROUPV2=true",
        "--test_env=GO_TEST_WRAP_TESTV=1", "--nocache_test_results", "--flaky_test_attempts=1",
        "--test_output=errors", "--remote_download_regex=.*test[.]log$",
    ]
    bep = ROOT / f"{arch}-{command}.bep"
    log = ROOT / f"{arch}-{command}.log"
    with log.open("w") as out:
        result = subprocess.run([*flags, "--build_event_json_file=" + str(bep), "--", *labels],
                                stdout=out, stderr=subprocess.STDOUT)
    tests, ends, actions = {}, {}, []
    invocation = ""
    digest = hashlib.sha256()
    with bep.open("rb") as stream:
        for line in stream:
            digest.update(line)
            event = json.loads(line)
            eid = event["id"]
            if "started" in event:
                invocation = event["started"]["uuid"]
            if "targetCompleted" in eid and not (end := eid["targetCompleted"]).get("aspect"):
                ends[end["label"]] = [end["configuration"]["id"], event.get("completed", {}).get("success", False)]
            if "action" in event and not event["action"].get("success", False):
                action = event["action"]
                actions.append([eid.get("actionCompleted", {}).get("label"), action.get("type"), action.get("exitCode")])
            if "testSummary" in event:
                summary = event["testSummary"]
                label = eid["testSummary"]["label"]
                body = Path("bazel-testlogs") / label.removeprefix("//").replace(":", "/").lstrip("/") / "test.log"
                entry = {"status": summary.get("overallStatus"), "attempts": summary.get("totalRunCount")}
                if body.is_file():
                    data = body.read_bytes()
                    entry["log_sha256"] = hashlib.sha256(data).hexdigest()
                    entry["excerpt"] = excerpt(data.decode(errors="replace").splitlines())
                tests[label] = entry
    console = log.read_text(errors="replace").splitlines()
    # Keep genrules' stderr for each distinct action; Bazel prints it immediately
    # following the ERROR line. The recipe uses set -x so its failed command is visible.
    action_context = {}
    for i, line in enumerate(console):
        if line.startswith("ERROR:") and any(label in line for label in labels):
            label = next(name for name in labels if name in line)
            following = []
            for step in console[i:i+65]:
                if following and (step.startswith("[") or step.startswith("ERROR:")):
                    break
                following.append(step)
            action_context[label] = excerpt(following)
    selected = {
        "source": SOURCE, "arch": arch, "command": command, "exit": result.returncode, "invocation": invocation,
        "bep_sha256": digest.hexdigest(), "targets": ends, "tests": tests, "failed_actions": actions,
        "action_stderr": action_context, "bazel_excerpt": excerpt(console),
    }
    (ROOT / "results" / f"{arch}-{command}.json").write_text(json.dumps(selected, indent=2, sort_keys=True)+"\n")
    print(arch, command, invocation, result.returncode,
          Counter(value[1] for value in ends.values()), Counter(v["status"] for v in tests.values()), flush=True)
    return set(labels) <= ends.keys() and (command != "test" or (
        set(labels) == tests.keys() and all(
            v["status"] == "PASSED" or "excerpt" in v for v in tests.values())))


if __name__ == "__main__":
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == SOURCE
    cov = record(sys.argv[1], "build", COVERAGE)
    tests = record(sys.argv[1], "test", TESTS)
    raise SystemExit(0 if cov and tests else 1)
