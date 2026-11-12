"""Run the supplier's Bazel jobs and retain their target and test results."""

from collections import Counter
import json
import os
from pathlib import Path
import subprocess

ROOT = Path("llvm").resolve()
E2E = ROOT / "e2e/rules_cc"
TMP = Path(os.environ["RUNNER_TEMP"])
OUT = TMP / "sidecar-evidence"
OUT.mkdir()
CANDIDATE = "14ebf27eb3eead0d725f66c0ff1f0f484b20fa8c"
STAGES = "baa2066bd9f6830d078228a9047649f8334f712e"
COMBINED = "b253312fd2f6b047d8a70781c7623f6e2c1d4064"
COMBINED_TREE = "03d14c94b198a69b918fad2f1e55b4ae818d4329"
FISSION = "//:asan_fission_debug_package_test"
STAGE = "//tests/runtime_stage:stage_independent_test"
STRIP = "--experimental_output_paths=strip"
FAILURES = []


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def check(name: str, cwd: Path, command: str, args: list[str], required_test: str = "") -> None:
    events = TMP / (name + ".bep")
    argv = ["bazel", f"--bazelrc={ROOT}/.github/workflows/ci.bazelrc", "--bazelrc=.bazelrc",
            f"--bazelrc={TMP}/sidecar.bazelrc", command, "--noannounce_rc", "--color=no", "--jobs=60",
            f"--build_event_json_file={events}", *args]
    process = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    diagnostics = []
    for line in process.stdout.splitlines():
        if any(key in line.lower() for key in ("api-key", "credential", "header=", "access_token")):
            continue
        if any(word in line for word in ("Invocation ID:", "ERROR:", "FAIL:", "Build completed", "did NOT complete", "processes:", "does not depend on a module", "fatal error:")):
            diagnostics.append(line)
    (OUT / (name + ".log")).write_text("\n".join(diagnostics) + "\n")
    tests = {}
    executions = []
    failed_targets = []
    successful_targets = 0
    invocation = None
    if events.is_file():
        with events.open() as source:
            for line in source:
                event = json.loads(line)
                identity = event["id"]
                if started := event.get("started"):
                    invocation = started.get("uuid")
                if "testSummary" in identity and "testSummary" in event:
                    tests[identity["testSummary"]["label"]] = event["testSummary"]["overallStatus"]
                if "testResult" in identity and identity["testResult"]["label"] == required_test:
                    result = event["testResult"]
                    executions.append({key: result[key] for key in ("status", "cachedLocally", "testAttemptDurationMillis") if key in result}
                                      | {"strategy": result.get("executionInfo", {}).get("strategy"),
                                         "cachedRemotely": result.get("executionInfo", {}).get("cachedRemotely", False)})
                if "targetCompleted" in identity and "completed" in event:
                    if event["completed"].get("success"):
                        successful_targets += 1
                    else:
                        failed_targets.append(identity["targetCompleted"]["label"])
    report = {"sourceHead": git("rev-parse", "HEAD"), "sourceTree": git("write-tree"),
              "bazel": os.environ["USE_BAZEL_VERSION"], "cwd": str(cwd.relative_to(ROOT)),
              "command": command, "arguments": args, "exit": process.returncode, "invocation": invocation,
              "testStatusCounts": dict(Counter(tests.values())), "tests": tests, "requiredTest": required_test,
              "requiredTestResults": executions, "successfulTargets": successful_targets, "failedTargets": failed_targets}
    (OUT / (name + ".json")).write_text(json.dumps(report, indent=2) + "\n")
    print(name, process.returncode, invocation, report["testStatusCounts"], successful_targets,
          failed_targets[:10], executions, flush=True)
    if process.returncode or not invocation or not successful_targets or (required_test and
            (tests.get(required_test) != "PASSED" or not any(e["status"] == "PASSED" and not e.get("cachedLocally")
             and not e["cachedRemotely"] for e in executions))):
        FAILURES.append(name)
        print("\n".join(diagnostics[-20:]), flush=True)


assert git("rev-parse", "HEAD") == CANDIDATE
assert git("rev-parse", "HEAD^") == STAGES
os.environ["USE_BAZEL_VERSION"] = (ROOT / ".bazelversion").read_text().strip()
version = subprocess.check_output(["bazel", "--version"], cwd=ROOT, text=True).strip()
assert version == "bazel " + os.environ["USE_BAZEL_VERSION"], version

check("stage", ROOT, "test", ["--nocache_test_results", STAGE], STAGE)
check("mapped", E2E, "test", [STRIP, "--config=cpp_path_mapping", "--nocache_test_results", "//..."], FISSION)
check("unmapped", E2E, "build", [STRIP, "//..."])
check("unmapped-fission", E2E, "test", [STRIP, "--nocache_test_results", FISSION], FISSION)

# Apply the identical candidate diff above the independently verified joint
# bootstrap fix so the real fission target also exercises both supplier PRs.
patch = subprocess.check_output(["git", "diff", STAGES, CANDIDATE], cwd=ROOT)
subprocess.run(["git", "fetch", "--quiet", "--no-tags", "--depth=1", "origin", COMBINED], cwd=ROOT, check=True)
subprocess.run(["git", "switch", "--quiet", "--detach", COMBINED], cwd=ROOT, check=True)
subprocess.run(["git", "apply", "--index", "-"], cwd=ROOT, input=patch, check=True)
assert git("write-tree") == COMBINED_TREE
check("combined-unmapped-fission", E2E, "test", [STRIP, "--nocache_test_results", FISSION], FISSION)
if FAILURES:
    raise SystemExit("Unqualified arms: " + ", ".join(FAILURES))
