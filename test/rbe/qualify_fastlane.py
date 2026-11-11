"""Compare the benchmark's dispatched examples, clock assertion and Linux runsc run."""

import base64
import collections
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import zlib

RECEIPTS = Path(os.environ["RUBY_RECEIPTS"])
IMAGE = Path("images/benchmarks/rubydev")
DOCKERFILE = IMAGE / "Dockerfile"
BUILD = Path("test/docker/BUILD")
CLOCK = "//test/docker:ruby_clock_test"
SUITE = "//test/benchmarks/fs:rubydev_test_continuous_systrap_owned"
BENCH = "^BenchmarkRubySpecTest$/^page_cache[.]dirty$/^filesystem[.]rootfs$"


def log_for(target: str) -> str:
    path = Path("bazel-testlogs", target[2:].replace(":", "/"), "test.log")
    return path.read_text(errors="replace") if path.is_file() else ""


def run(phase: str) -> dict:
    result = RECEIPTS / phase
    result.mkdir()
    args = ["bazel", "--bazelrc=" + os.environ["RUBY_RC"], "test",
            "--extra_execution_platforms=//tools/bazeldefs:rbe_linux_amd64",
            "--host_platform=//tools/bazeldefs:rbe_linux_amd64",
            "--spawn_strategy=remote", "--strategy=TestRunner=remote",
            "--jobs=100", "--define=rbe=true", "--remote_download_outputs=toplevel", "--keep_going",
            "--nocache_test_results", "--flaky_test_attempts=1", "--test_output=errors",
            "--test_arg=-test.bench=" + BENCH, "--test_arg=-test.benchtime=1x",
            "--build_tag_filters=", "--test_tag_filters=", "--build_event_json_file=" + str(result / "bep"),
            CLOCK, SUITE]
    with (result / "bazel.log").open("w") as log:
        code = subprocess.run(args, stdout=log, stderr=subprocess.STDOUT).returncode
    targets = {}
    for line in (result / "bep").read_text().splitlines():
        event = json.loads(line)
        if summary := event.get("testSummary"):
            targets[event["id"]["testSummary"]["label"]] = summary["overallStatus"]
    clock = log_for(CLOCK) if CLOCK in targets else ""
    suite = log_for(SUITE) if SUITE in targets else ""
    counts = re.findall(r"(\d+) examples?, (\d+) failures?", suite)
    last = re.search(r"^RUBY_EXAMPLES ([\w+/=]+)$", clock, re.M)
    examples = json.loads(zlib.decompress(base64.b64decode(last[1]))) if last else []
    (result / "examples.json").write_text(json.dumps(examples))
    (result / "clock.log").write_text(re.sub(r"^RUBY_EXAMPLES .*$", "[inventory in examples.json]", clock, flags=re.M))
    (result / "suite.log").write_text(suite)
    facts = {"phase": phase, "exit": code, "targets": targets, "examples": len(examples),
             "clock": re.findall(r"^DECODE_BOUNDARY .+$", clock, re.M),
             "native_result": re.findall(r"\d+ examples?, \d+ failures?", clock)[-1:],
             "benchmark": re.findall(r"^BenchmarkRubySpecTest/page_cache\.dirty/filesystem\.rootfs[^\n]*", suite, re.M),
             "output_counts": sorted(set(counts))}
    print(facts, flush=True)
    raw = (result / "bazel.log").read_text(errors="replace")
    # Keep only public diagnostics in the receipt if Bazel fails before launching a target.
    public = [line[:3000] for line in raw.splitlines() if re.match(r"^(ERROR:|FAILED:|.*Invocation ID:|.*Target //.*failed to build|.*sh:.*(not found|failed)|.*failed to solve)", line)]
    (result / "action-errors.txt").write_text("\n".join(public[-35:]))
    return facts


def main() -> None:
    RECEIPTS.mkdir(parents=True, exist_ok=True)
    base_build = BUILD.read_text()
    base_dockerfile = DOCKERFILE.read_text()
    summary = {}
    for phase in ("stock", "upgrade", "backport"):
        if phase != "stock":
            BUILD.write_text(base_build.replace("    source_archives = {", "    source_archives = {\n        \"benchmarks/rubydev\": {\"amd64\": \":ruby_source_amd64\"},", 1))
        if phase == "upgrade":
            DOCKERFILE.write_text(base_dockerfile.replace("2.207.0.tar.gz", "2.220.0.tar.gz"))
        if phase == "backport":
            shutil.copyfile("test/rbe/fastlane_token_time.patch", IMAGE / "token-time.patch")
            DOCKERFILE.write_text(base_dockerfile + "RUN cd /fastlane && patch -p1 < /files/token-time.patch\n")
        summary[phase] = run(phase)
        (RECEIPTS / "summary.json").write_text(json.dumps(summary, indent=2))
    for phase in ("stock", "upgrade", "backport"):
        phase_result = summary[phase]
        assert phase_result["clock"] and phase_result["benchmark"], phase_result
        assert phase_result["native_result"] == (["1 example, 1 failure"] if phase == "stock" else ["1 example, 0 failures"]), phase_result
    def cases(phase: str) -> collections.Counter:
        return collections.Counter(map(tuple, json.loads((RECEIPTS / phase / "examples.json").read_text())))
    base, upgrade, backport = map(cases, ("stock", "upgrade", "backport"))
    delta = {"upgrade_removed": sorted((base - upgrade).elements()),
             "upgrade_added": sorted((upgrade - base).elements()),
             "backport_same": backport == base}
    (RECEIPTS / "workload-delta.json").write_text(json.dumps(delta, indent=2))
    assert base.total() == 3613 and backport == base, (base.total(), backport.total())
    assert summary["backport"]["targets"].get(SUITE) == "PASSED", summary["backport"]


if __name__ == "__main__":
    main()
