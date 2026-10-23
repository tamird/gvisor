"""Hash the state generator's raw and formatted outputs for the same owners."""
import hashlib
import json
import os
from pathlib import Path
import shlex


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    packages = ("pkg/state/tests", "pkg/sentry/kernel", "pkg/sentry/fsimpl/tmpfs",
                "pkg/tcpip/transport/tcp", "pkg/tcpip/transport/udp")
    owners = []
    for package in packages:
        stem = package.rsplit("/", 1)[1] + "_state_autogen"
        owners.extend(f"//{package}:{stem}{suffix}" for suffix in ("_with_imports", ""))
    generator = "//tools/go_stateify:stateify_test"
    graphs = "//pkg/state/tests:tests_test"
    if config["targets"] != [*owners, generator, graphs]:
        raise ValueError("Unexpected generator qualification selection")
    options = ["--config=x86_64", "--compilation_mode=opt", "--lockfile_mode=error",
               "--remote_download_outputs=all", "--noannounce_rc"]

    def check(name, args, kind, targets):
        phase = invoke(name, args, kind, targets=targets)
        if phase["status"] != "PASS":
            raise RuntimeError(f"{name} failed")

    check("generated-files", ["build", *options, *owners], "build", owners)
    outputs = {}
    for owner in owners:
        package, name = owner.removeprefix("//").split(":")
        data = (Path("bazel-bin") / package / (name + ".go")).read_bytes()
        if b"StateSave(" not in data or b"StateDecode(" not in data:
            raise ValueError(f"Missing generated methods: {owner}")
        outputs[owner] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    (evidence / "generated-files.json").write_text(json.dumps(outputs, sort_keys=True) + "\n")

    formatter = shlex.join(["bash", "-c", 'set -eu; export PATH="$(dirname "$1"):$PATH"; '
                           'cd "$BUILD_WORKSPACE_DIRECTORY"; exec bash tools/lint.sh gofmt', "--"])
    check("gofmt", ["run", *options, "--run_under=" + formatter, "@io_bazel_rules_go//go:go"],
          "command", [])
    tests = ["test", *options, "--strategy=TestRunner=local", "--nocache_test_results",
             "--runs_per_test=1", "--flaky_test_attempts=1", "--test_env=GO_TEST_WRAP=1",
             "--test_env=GO_TEST_WRAP_TESTV=1"]
    check("generator", [*tests, generator], "test", [generator])
    check("graph", [*tests, "--test_filter=^TestCodec(RandomGraphs|AbsorbedReferrer|Standalone|NestedMaps)$",
                    graphs], "test", [graphs])
