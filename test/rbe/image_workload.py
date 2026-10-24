"""Run the state codec suites and analyzers for the array repair."""
import json
import os
from pathlib import Path
import shlex


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    owners = ["//pkg/state:state_test", "//pkg/state/tests:tests_test",
              "//pkg/state:state_nogo", "//pkg/state/tests:tests_nogo",
              "//pkg/state/tests:tests_test_nogo"]
    if config["targets"] != owners:
        raise ValueError("Unexpected state repair owners")
    options = ["--config=x86_64", "--compilation_mode=opt", *config["consumer_options"],
               "--lockfile_mode=error", "--remote_download_outputs=all", "--noannounce_rc"]
    formatter = shlex.join(["bash", "-c", 'set -eu; export PATH="$(dirname "$1"):$PATH"; '
                           'cd "$BUILD_WORKSPACE_DIRECTORY"; exec bash tools/lint.sh gofmt', "--"])
    formatted = invoke("gofmt", ["run", *options, "--run_under=" + formatter,
                                "@io_bazel_rules_go//go:go"], "command", targets=[])
    if formatted["status"] != "PASS":
        raise RuntimeError("Canonical Go formatting failed")
    # The complete suites retain graph, hook, alias and format coverage;
    # native failures remain failures independently of result projection.
    invoke("byte-array", ["test", *options, "--strategy=TestRunner=local",
                          "--nocache_test_results", "--runs_per_test=1", "--flaky_test_attempts=1",
                          "--test_output=all", "--test_env=GO_TEST_WRAP=1",
                          "--test_env=GO_TEST_WRAP_TESTV=1",
                          *owners], "test", targets=owners)
