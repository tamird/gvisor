"""Measure the generated leaf walk in the sentry and the existing checkpoint fixture."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess


def run(invoke, record_execution, evidence: Path, private: Path, output_root: Path) -> None:
    config = json.loads(os.environ["APPLICABILITY_QUALIFICATION_CONFIG"])
    sentry = "//runsc/cmd/sentry:gvisor_sentry"
    generator = "//tools/go_stateify:stateify_test"
    graphs = "//pkg/state/tests:tests_test"
    package = "//pkg/state/tests:tests_state_autogen"
    if config["targets"] != [generator, graphs, package, sentry]:
        raise ValueError("Unexpected state leaf experiment owners")
    options = ["--config=x86_64", "--compilation_mode=opt", "--nostamp", "--lockfile_mode=error",
               "--remote_download_outputs=toplevel", "--noannounce_rc"]

    def check(name, args, kind, targets):
        verdict = invoke(name, args, kind, targets=targets)
        if verdict["status"] != "PASS":
            raise RuntimeError(f"{name} failed")

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
    check("generated", ["build", *options, package], "build", [package])
    generated = (Path("bazel-bin/pkg/state/tests/tests_state_autogen.go")).read_text()
    shared = bool(re.search(r"func \(\w+ \*benchFields\) stateVisitFields\(", generated))
    if shared != config["visitor"]:
        raise ValueError("Existing fields benchmark did not select the expected generation path")
    check("sentry", ["build", *options, sentry], "build", [sentry])
    check("sentry-path", ["cquery", *options, "--output=files", sentry], "command", [])
    files = (evidence / "invocation-sentry-path/stdout.log").read_text().splitlines()
    if len(files) != 1 or Path(files[0]).name != "gvisor_sentry":
        raise ValueError(f"Unexpected sentry output: {files}")
    binary = Path(files[0]); sections = subprocess.check_output(["size", "-A", "-d", binary], text=True)
    text_size = int(re.search(r"^\.text\s+(\d+)\s+\d+", sections, re.MULTILINE).group(1))
    with binary.open("rb") as raw:
        digest = hashlib.file_digest(raw, "sha256").hexdigest()
    (evidence / "leaf-size.json").write_text(json.dumps({"source": config["source"], "product": config["product"],
        "binary_bytes": binary.stat().st_size, "text_bytes": text_size, "sha256": digest,
        "fixture_uses_shared_walk": shared}) + "\n")

    # The two arms run on separate workers. Compare allocations here; timing
    # comparisons require running both binaries on one worker.
    check("fields", ["run", *options, graphs, "--", "-test.run=^$",
                     "-test.bench=^(BenchmarkEncoding|BenchmarkDecoding)$/^fields$/^(state|state-reflect)$",
                     "-test.benchmem", "-test.benchtime=250ms", "-test.count=4"], "command", [])
    rows = re.findall(r"^(Benchmark(?:En|De)coding/fields/(?:state|state-reflect))(?:-\d+)?\s+\d+\s+[\d.]+ ns/op\s+(\d+) B/op\s+(\d+) allocs/op$",
                      (evidence / "invocation-fields/stdout.log").read_text(), re.MULTILINE)
    expected = {f"Benchmark{op}/fields/{backend}" for op in ("Encoding", "Decoding") for backend in ("state", "state-reflect")}
    if any(sum(name == want for name, _, _ in rows) != 4 for want in expected) or len(rows) != 16:
        raise ValueError(f"Missing checkpoint benchmark observations: {rows}")
    (evidence / "leaf-allocations.json").write_text(json.dumps(rows) + "\n")
