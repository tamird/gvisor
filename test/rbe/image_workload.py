"""Run the focused SACK accounting regression through its existing owner."""
import json
import os
from pathlib import Path
import shlex


OWNER = "//pkg/tcpip/transport/tcp/test/e2e:tcp_rack_test"


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    if config["targets"] != [OWNER]:
        raise ValueError("Unexpected SACK regression owner")
    options = ["--config=x86_64", "--compilation_mode=opt", *config["consumer_options"],
               "--lockfile_mode=error", "--remote_download_outputs=all", "--noannounce_rc"]
    formatter = shlex.join(["bash", "-c", 'set -eu; export PATH="$(dirname "$1"):$PATH"; '
                           'cd "$BUILD_WORKSPACE_DIRECTORY"; exec bash tools/lint.sh gofmt', "--"])
    formatted = invoke("gofmt", ["run", *options, "--run_under=" + formatter,
                                "@io_bazel_rules_go//go:go"], "command", targets=[])
    if formatted["status"] != "PASS":
        raise RuntimeError("Canonical Go formatting failed")
    selected = ["--strategy=TestRunner=local", "--nocache_test_results", "--flaky_test_attempts=1",
                "--test_output=all", "--test_timeout=300", "--test_env=GO_TEST_WRAP=1",
                "--test_env=GO_TEST_WRAP_TESTV=1"]
    checked = invoke("sacked-out-regression", ["test", *options, *selected,
                     "--runs_per_test=1", "--test_arg=-test.run=^TestRACKUpdateSackedOut$", OWNER],
                     "test", targets=[OWNER])
    if checked["status"] != "PASS":
        raise RuntimeError("SACK accounting regression failed; preserve native test evidence")
