#!/bin/bash

# Copyright 2023 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Script to easily run GPU tests on all supported driver versions. This should
# be run from the gVisor repo root directory.
set -ueo pipefail

# The public Make adapter and declared Bazel frontend share this loop and the
# driver CLI's sorted, architecture-filtered registry. The declared path runs
# already-built executables; it never invokes another build system.
if [[ ${1:-} == --declared ]]; then
  [[ $# -eq 7 ]]
  driver_tool=$2
  my_shard=$3
  total_shards=$4
  shift 4
  smoke_commands=("$@")
  [[ -d ${TEST_UNDECLARED_OUTPUTS_DIR:?} ]]
  run_driver() { "$driver_tool" "$@"; }
  run_smoke() {
    local command index=0
    local output=${TEST_UNDECLARED_OUTPUTS_DIR:?}/driver-$1
    mkdir -p "$output"
    for command in "${smoke_commands[@]}"; do
      local step=$output/$index
      mkdir "$step"
      (
        # The outer action owns its deadline and complete cohort selection.
        # These raw binaries have no nested Bazel TestRunner.
        unset TEST_TIMEOUT TESTBRIDGE_TEST_ONLY TESTBRIDGE_TEST_RUNNER_FAIL_FAST
        unset TEST_TOTAL_SHARDS TEST_SHARD_INDEX TEST_SHARD_STATUS_FILE
        TEST_UNDECLARED_OUTPUTS_DIR="$step" XML_OUTPUT_FILE="$step/test.xml" "$command"
      )
      index=$((index + 1))
    done
  }
else
  [[ $# -eq 0 ]]
  # Buildkite's public driver partitions are zero-based.
  my_shard=${BUILDKITE_PARALLEL_JOB:-0}
  total_shards=${BUILDKITE_PARALLEL_JOB_COUNT:-1}
  run_driver() { make sudo TARGETS=tools/gpu:main ARGS="$*"; }
  run_smoke() { make gpu-smoke-tests; }
fi
[[ $my_shard =~ ^[0-9]+$ && $total_shards =~ ^[0-9]+$ ]]
(( total_shards > 0 && my_shard < total_shards ))

record() {
  printf 'GPU_DRIVER %s %s %s\n' "$1" "$2" "${3:-}" >&2
  if [[ -n ${TEST_UNDECLARED_OUTPUTS_DIR:-} ]]; then
    printf '%s\t%s\t%s\n' "$1" "$2" "${3:-}" >> "$TEST_UNDECLARED_OUTPUTS_DIR/drivers.tsv"
  fi
}

tmp_file="$(mktemp)"
trap 'rm -f "$tmp_file"' EXIT
run_driver list --outfile="$tmp_file"
read -r -a all_versions <<< "$(cat "$tmp_file")"
if [[ ${#all_versions[@]} -eq 0 ]]; then
  echo 'No driver versions found.' >&2
  exit 1
fi

counter=0
versions=()
for driver in "${all_versions[@]}"; do
  modulo="$(( "$counter" % "$total_shards" ))"
  explanation="${counter} % ${total_shards} == ${modulo}; we are shard ${my_shard} of $(( ${total_shards} - 1 ))"
  if [[ "$modulo" -eq "$my_shard" ]]; then
    echo "Will test driver ${driver} ($explanation)" >&2
    versions+=("$driver")
  else
    echo "Skipping driver ${driver} ($explanation)" >&2
  fi
  counter="$(( "$counter" + 1 ))"
done

if [[ "${#versions[@]}" -eq 0 ]]; then
  echo "No versions to test on this shard (we are shard ${my_shard} of $(( ${total_shards} - 1 )))." >&2
  record - empty-shard
  exit 0
fi

num_successful=0
for driver in "${versions[@]}"; do
  if run_driver install --version "$driver"; then
    record "$driver" installed
  else
    install_exit_code=$?
    record "$driver" install-failed "$install_exit_code"
    echo "Installing driver ${driver} failed. Not testing this version." >&2
    continue
  fi
  # A conditional function call disables errexit throughout its body. Run the
  # complete smoke sequence with errexit enabled, then inspect that result.
  set +e
  (set -e; run_smoke "$driver")
  smoke_exit_code=$?
  set -e
  if (( smoke_exit_code != 0 )); then
    record "$driver" smoke-failed "$smoke_exit_code"
    exit "$smoke_exit_code"
  fi
  record "$driver" passed
  num_successful="$(( "$num_successful" + 1 ))"
done
if [[ "$num_successful" == 0 ]]; then
  echo 'No version was successfully tested.' >&2
  exit 1
fi
