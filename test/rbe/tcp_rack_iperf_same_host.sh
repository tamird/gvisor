#!/bin/bash
# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Fork-only comparison of the existing complete single-connection workload.
# actions.sh owns credentials, the private Docker bridge and compact capture.
set -euo pipefail
[[ ${RACK_BENCHMARK_HOST:?} =~ ^[01]$ ]]
: "${qualification_root_bazel:?}"
owner=//test/benchmarks/network:iperf_test_continuous_systrap_owned
report=$RUNNER_TEMP/qualification/iperf
mkdir -p "$report"
after=$(git rev-parse HEAD)
before=$(git rev-parse HEAD^)
original_ref=$(git symbolic-ref -q HEAD || true)
[[ $after == "$QUALIFICATION_COMMIT" ]]
[[ $(git rev-parse "$before^") == 27e04f934c909ee4196d7ec06aacd14e23e2cd85 ]]
git status --porcelain --untracked-files=no > "$report/source-before.txt"
test ! -s "$report/source-before.txt"
git diff --binary "$before" "$after" > "$report/production.patch"
git diff --raw --no-abbrev --no-renames "$before" "$after" > "$report/production-objects.txt"
printf '%s  %s\n' \
  260e03ae1bf9325c3afa15a82a88e6799915f87a5968d94f01034b2c33a1534b \
  "$report/production-objects.txt" | sha256sum --check --strict
printf 'before=%s\nafter=%s\n' "$before" "$after" > "$report/sources.txt"
source_paths=(
  pkg/tcpip/transport/tcp test/benchmarks/network test/benchmarks/harness
  test/benchmarks/tools test/metricsviz pkg/test/dockerutil test/docker test/benchmarks/defs.bzl
  test/rbe/tcp_rack_iperf_same_host.sh test/rbe/actions.sh test/rbe/qualify.sh
  test/rbe/docker_setup.sh .github/workflows/build.yml MODULE.bazel
  MODULE.bazel.lock go.mod .bazelrc tools/defs.bzl tools/rules_go_symbols.patch
  BUILD tools/release.bzl runsc/BUILD runsc/cmd/sentry images/default/bazelversion
)
for side in before after; do
  revision=$before
  if [[ $side == after ]]; then revision=$after; fi
  git cat-file -p "$revision" > "$report/$side.commit"
  git ls-tree -r "$revision" -- "${source_paths[@]}" > "$report/$side.source-objects"
  git archive --format=tar "$revision" "${source_paths[@]}" | gzip -n > "$report/$side.sources.tar.gz"
done
test_options=("$@" --test_output=all --local_test_jobs=1 --nocache_test_results --runs_per_test=1 --flaky_test_attempts=1)
# Test inherits BuildCommand's execution/configuration options in Bazel8.8.1.
# Preserve the actual run_under and local-test configuration when prebuilding.
build_options=("${test_options[@]}")
raw_bep=

finish() {
  local status=$? cleanup_status=0
  local quiesced=true
  set +e
  trap '' TERM INT
  trap - EXIT
  if (( status != 0 )); then
    # Stop this job's Bazel server before restoring a checkout after a signal.
    timeout --signal=TERM --kill-after=5s 30s bash -c 'bazel shutdown' \
      > "$report/shutdown.log" 2>&1 || quiesced=false
  fi
  # Both sources are immutable and clean. Never overwrite unrelated changes.
  git status --porcelain --untracked-files=no > "$report/source-at-exit.txt" || quiesced=false
  if [[ $quiesced == true ]] && test ! -s "$report/source-at-exit.txt"; then
    if [[ -n $original_ref ]]; then
      git switch "${original_ref#refs/heads/}" || cleanup_status=1
    else
      git switch --detach "$after" || cleanup_status=1
    fi
  else
    cleanup_status=1
  fi
  git rev-parse HEAD > "$report/final-head.txt" || cleanup_status=1
  [[ $(cat "$report/final-head.txt") == "$after" ]] || cleanup_status=1
  git status --porcelain --untracked-files=no > "$report/source-after.txt" || cleanup_status=1
  test ! -s "$report/source-after.txt" || cleanup_status=1
  if [[ -n $raw_bep ]]; then rm -f "$raw_bep" || cleanup_status=1; fi
  printf '%s\n' "$status" > "$report/primary-exit.txt" || cleanup_status=1
  printf '%s\n' "$cleanup_status" > "$report/cleanup-exit.txt" || cleanup_status=1
  if (( status == 0 )); then status=$cleanup_status; fi
  exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

set_source() {
  local side=$1 revision
  case "$side" in before) revision=$before ;; after) revision=$after ;; *) return 2 ;; esac
  git status --porcelain --untracked-files=no > "$report/source-transition.txt"
  test ! -s "$report/source-transition.txt"
  git switch --detach "$revision"
  [[ $(git rev-parse HEAD) == "$revision" ]]
}

phase() {
  local name=$1 command=$2 status=0 capture_status=0 revision
  local -a run_evidence=() pipeline_status=()
  shift 2
  revision=$(git rev-parse HEAD)
  export QUALIFICATION_PHASE=$name
  raw_bep=$(mktemp "$RUNNER_TEMP/iperf-bep.XXXXXX")
  printf '%s\n' "$revision" > "$report/$name.source"
  printf '%s\n' "$command" "$@" > "$report/$name.arguments"
  if [[ $command == run ]]; then
    # The Actions wrapper captures build/test. A run option must precede the
    # executable's -- separator, so it cannot be appended by that wrapper.
    run_evidence=("--execution_log_compact_file=$report/$name.binpb")
  fi
  set +e
  bazel "$command" --noannounce_rc --color=no --curses=no \
    "${run_evidence[@]}" \
    "--build_metadata=COMMIT_SHA=$revision" \
    "--build_metadata=REPO_URL=$GITHUB_SERVER_URL/$GITHUB_REPOSITORY" \
    "--build_metadata=BRANCH_NAME=$GITHUB_REF_NAME" \
    "--build_event_json_file=$raw_bep" --build_event_json_file_path_conversion=false "$@" 2>&1 | tee "$report/$name.log"
  pipeline_status=("${PIPESTATUS[@]}")
  set -e
  status=${pipeline_status[0]}
  if (( pipeline_status[1] != 0 )); then capture_status=1; fi
  printf '%s\n' "$status" > "$report/$name.exit" || capture_status=1
  # Retain partial result events and advertised outputs before checking them.
  # Raw options may contain credentials; only selected controls are retained.
  local -a collector=(timeout --signal=TERM --kill-after=5s 60s python3)
  if [[ $qualification_root_bazel == true ]]; then
    # Root Bazel places reports beneath /root; use its identity to read them.
    collector=(sudo -n "${collector[@]}")
  fi
  "${collector[@]}" - "$raw_bep" "$report" "$name" "$command" "$owner" <<'PY' || capture_status=$?
import json
from pathlib import Path
import sys
import shutil
import xml.etree.ElementTree as ET
from urllib.parse import unquote, urlsplit

raw, directory, name, command, owner = sys.argv[1:]
out = Path(directory)
events = []
partial = False
for line in Path(raw).read_text().splitlines():
    try:
        events.append(json.loads(line))
    except json.JSONDecodeError:
        partial = True
        break
keys = ("expanded", "configured", "completed", "aborted", "namedSetOfFiles",
        "testResult", "testSummary", "finished", "buildMetrics")
(out / (name + ".events.jsonl")).write_text("".join(
    json.dumps(e) + "\n" for e in events if any(k in e for k in keys)))
options = [e["optionsParsed"] for e in events if "optionsParsed" in e]
controls = [arg for event in options for arg in event["cmdLine"] if arg.startswith((
    "--jobs=", "--spawn_strategy=", "--strategy=", "--local_test_jobs=",
    "--runs_per_test=", "--flaky_test_attempts=", "--cache_test_results=",
    "--test_timeout=", "--test_filter=", "--test_arg=", "--run_under=",
    "--test_sharding_strategy=", "--build_metadata=")) or arg in (
        "--nocache_test_results", "--noremote_local_fallback")]
(out / (name + ".controls.json")).write_text(json.dumps(controls, indent=2) + "\n")
results = [e for e in events if "testResult" in e]
# Copy every advertised report for this owner before checking completeness or
# success, so a failed or interrupted phase retains its original diagnostics.
copied = {}
for result in results:
    identity = result["id"]["testResult"]
    if identity["label"] != owner:
        continue
    for report in result["testResult"].get("testActionOutput", []):
        if report["name"] not in ("test.log", "test.xml"):
            continue
        uri = urlsplit(report["uri"])
        assert uri.scheme == "file", uri.scheme
        source = Path(unquote(uri.path)).resolve()
        source.relative_to(Path("bazel-out").resolve())
        attempt = "-".join(f"{key}-{identity.get(key, 1)}" for key in ("run", "shard", "attempt"))
        target = out / name / attempt / report["name"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        copied.setdefault(report["name"], []).append(target)
assert not partial and options, (partial, len(options))
assert {"--jobs=400", "--spawn_strategy=remote", "--noremote_local_fallback"} <= set(controls), controls
if command != "test":
    assert not results, results
else:
    result, = results
    log_path, = copied["test.log"]
    xml_path, = copied["test.xml"]
    xml = ET.fromstring(xml_path.read_bytes())
    assert not any(list(xml.iter(tag)) for tag in ("failure", "error", "skipped")), name
    assert all(int(node.get(key, "0")) == 0 for node in xml.iter()
               for key in ("failures", "errors", "skipped")), name
    log = log_path.read_text().replace("\x16", "")
    assert "\nPASS\n" in log and "\nFAIL\n" not in log, name
    identity = result["id"]["testResult"]
    assert identity["label"] == owner, identity
    assert all(identity.get(k, 1) == 1 for k in ("run", "shard", "attempt")), identity
    body = result["testResult"]
    assert body["status"] == "PASSED", body
    assert not body.get("cachedLocally") and not body.get("executionInfo", {}).get("cachedRemotely"), body
    assert body["executionInfo"]["strategy"] == "docker", body
    completed, = [e["completed"] for e in events if e["id"].get("targetCompleted", {}).get("label") == owner and e.get("completed", {}).get("testTimeoutSeconds")]
    assert int(completed["testTimeoutSeconds"]) == 3600, completed
    assert [a for a in controls if a.startswith("--local_test_jobs=")][-1] == "--local_test_jobs=1", controls
    assert {"--strategy=TestRunner=docker", "--runs_per_test=1", "--flaky_test_attempts=1"} <= set(controls), controls
    assert any(a in controls for a in ("--nocache_test_results", "--cache_test_results=0", "--cache_test_results=false")), controls
    assert not any(a.startswith(("--test_timeout=", "--test_filter=", "--test_arg=", "--test_sharding_strategy=")) for a in controls), controls
PY
  printf '%s\n' "$capture_status" > "$report/$name.capture-exit" || capture_status=1
  rm -f "$raw_bep" || capture_status=1
  raw_bep=
  if (( status != 0 )); then return "$status"; fi
  return "$capture_status"
}

# Compile both sources before either warmup or measured sample. Compiler work
# remains remote; actual per-phase input/producer joins are checked afterward.
phase build-after build "${build_options[@]}" @org_golang_x_perf//cmd/benchstat
set_source before
phase build-before build "${build_options[@]}"
for side in before after; do
  set_source "$side"
  phase "warmup-$side" test "${test_options[@]}"
  # The owned daemon consumes this release layout. Retain both the CLI and
  # Sentry ELF; TCP layouts live in the latter. Bind their original bytes to
  # every measured native action after collection.
  sudo -n cp bazel-bin/release/runsc "$report/runsc-$side"
  sudo -n cp bazel-bin/release/gvisor-bin/gvisor_sentry "$report/sentry-$side"
  sha256sum "$report/runsc-$side" "$report/sentry-$side" > "$report/runtime-$side.sha256"
done
for ((pair = 0; pair < 12; pair++)); do
  order=(before after)
  if (( (pair + RACK_BENCHMARK_HOST) % 2 )); then order=(after before); fi
  for side in "${order[@]}"; do
    set_source "$side"
    printf -v name 'sample-%02d-%s' "$pair" "$side"
    phase "$name" test "${test_options[@]}"
  done
done
set_source after
timeout --signal=TERM --kill-after=5s 30s python3 - "$report" <<'PY'
import json
import math
from pathlib import Path
import re
import sys

out = Path(sys.argv[1])
rows = []
files = {"before": [], "after": []}
for pair in range(12):
    for side in ("before", "after"):
        phase = f"sample-{pair:02d}-{side}"
        lines = [line for line in (out / (phase + ".log")).read_text().splitlines()
                 if re.match(r"^BenchmarkIperfOneConnection/operation\.(Upload|Download)-\d+\s+\d+\s+", line)]
        assert len(lines) == 2, (phase, lines)
        assert {line.split()[0].rsplit("-", 1)[0] for line in lines} == {
            "BenchmarkIperfOneConnection/operation.Upload",
            "BenchmarkIperfOneConnection/operation.Download"}, lines
        for line in lines:
            words = line.split()
            assert int(words[1]) > 0 and len(words[2:]) % 2 == 0, line
            metrics = {unit: float(value) for value, unit in zip(words[2::2], words[3::2])}
            assert {"ns/op", "MB/s", "bandwidth.bytes_per_second"} <= metrics.keys(), line
            assert all(math.isfinite(value) and value > 0 for value in metrics.values()), line
            rows.append({"pair": pair, "side": side, "phase": phase,
                         "name": words[0], "iterations": int(words[1]), "metrics": metrics})
        files[side].extend(lines)
(out / "samples.json").write_text(json.dumps(rows, indent=2) + "\n")
for side, lines in files.items():
    assert len(lines) == 24, (side, len(lines))
    (out / (side + ".bench")).write_text(
        "goos: linux\ngoarch: amd64\npkg: gvisor.dev/gvisor/test/benchmarks/network\n"
        + "\n".join(lines) + "\n")
PY
phase benchstat run --config=rbe --config=x86_64 @org_golang_x_perf//cmd/benchstat \
  -- "$report/before.bench" "$report/after.bench"
