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
owner=//test/benchmarks/network:iperf_test_continuous_systrap_owned
report=$RUNNER_TEMP/qualification/iperf
mkdir -p "$report"
after=$(git rev-parse HEAD)
before=$(git rev-parse HEAD^)
original_ref=$(git symbolic-ref -q HEAD || true)
[[ $after == "$QUALIFICATION_COMMIT" ]]
[[ $(git rev-parse "$before^") == a02cf3f881b14b284bd67651f11ee56c4ec9e75e ]]
git status --porcelain --untracked-files=no > "$report/source-before.txt"
test ! -s "$report/source-before.txt"
git diff --binary "$before" "$after" > "$report/production.patch"
git diff --raw --no-abbrev --no-renames "$before" "$after" > "$report/production-objects.txt"
printf '%s  %s\n' \
  3631f6747a66a689cabfa3ee119742efc2a0f0605548cddfd8a5e9980718ada3 \
  "$report/production-objects.txt" | sha256sum --check --strict
printf 'before=%s\nafter=%s\n' "$before" "$after" > "$report/sources.txt"
test_options=("$@" --test_output=all --local_test_jobs=1 --nocache_test_results --runs_per_test=1 --flaky_test_attempts=1)
# Test inherits BuildCommand's execution/configuration options in Bazel8.8.1.
# Preserve the actual run_under and local-test configuration when prebuilding.
build_options=("${test_options[@]}")
raw_bep=

finish() {
  local status=$?
  local quiesced=true
  trap '' TERM INT
  trap - EXIT
  if (( status != 0 )); then
    # Stop this job's Bazel server before restoring a checkout after a signal.
    timeout --signal=TERM --kill-after=5s 30s bash -c 'bazel shutdown' \
      > "$report/shutdown.log" 2>&1 || quiesced=false
  fi
  # Both sources are immutable and clean. Never overwrite unrelated changes.
  git status --porcelain --untracked-files=no > "$report/source-at-exit.txt"
  if [[ $quiesced == true ]] && test ! -s "$report/source-at-exit.txt"; then
    if [[ -n $original_ref ]]; then
      git switch "${original_ref#refs/heads/}" || status=1
    else
      git switch --detach "$after" || status=1
    fi
  else
    status=1
  fi
  git rev-parse HEAD > "$report/final-head.txt"
  [[ $(< "$report/final-head.txt") == "$after" ]] || status=1
  git status --porcelain --untracked-files=no > "$report/source-after.txt"
  test ! -s "$report/source-after.txt" || status=1
  [[ -z $raw_bep ]] || rm -f "$raw_bep"
  printf '%s\n' "$status" > "$report/exit.txt"
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
  local -a run_evidence=()
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
  bazel "$command" --noannounce_rc --color=no --curses=no \
    "${run_evidence[@]}" \
    "--build_metadata=COMMIT_SHA=$revision" \
    "--build_metadata=REPO_URL=$GITHUB_SERVER_URL/$GITHUB_REPOSITORY" \
    "--build_metadata=BRANCH_NAME=$GITHUB_REF_NAME" \
    "--build_event_json_file=$raw_bep" "$@" 2>&1 | tee "$report/$name.log" || status=$?
  printf '%s\n' "$status" > "$report/$name.exit"
  # Retain partial result events and advertised outputs before checking them.
  # Raw options may contain credentials; only selected controls are retained.
  timeout --signal=TERM --kill-after=5s 60s python3 - "$raw_bep" "$report" "$name" "$command" "$owner" <<'PY' || capture_status=$?
import json
from pathlib import Path
import sys

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
assert not partial and options, (partial, len(options))
assert {"--jobs=400", "--spawn_strategy=remote", "--noremote_local_fallback"} <= set(controls), controls
results = [e for e in events if "testResult" in e]
if command != "test":
    assert not results, results
else:
    result, = results
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
  printf '%s\n' "$capture_status" > "$report/$name.capture-exit"
  rm -f "$raw_bep"
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
done
for pair in 0 1 2 3 4 5; do
  order=(before after)
  if (( pair % 2 )); then order=(after before); fi
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
for pair in range(6):
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
    assert len(lines) == 12, (side, len(lines))
    (out / (side + ".bench")).write_text(
        "goos: linux\ngoarch: amd64\npkg: gvisor.dev/gvisor/test/benchmarks/network\n"
        + "\n".join(lines) + "\n")
PY
phase benchstat run --config=rbe --config=x86_64 @org_golang_x_perf//cmd/benchstat \
  -- "$report/before.bench" "$report/after.bench"
