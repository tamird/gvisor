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

# Run already-built GPU frontends serially on an exclusively owned host.
# Each frontend owns and tears down its Docker daemon before returning.
set -euo pipefail
[[ $# -ge 3 ]]
mode=$1
driver_tool=$2
shift 2
output=${TEST_UNDECLARED_OUTPUTS_DIR:?}
[[ -d $output ]]
# BUILD owns each command list; validate the mode before host mutation.
case "$mode" in
  smoke|all|l4|cuda|cos) ;;
  *) echo "Unknown GPU cohort: $mode" >&2; exit 1 ;;
esac
if [[ $mode != cos ]]; then
  "$driver_tool" install --latest
fi
index=0
for command in "$@"; do
  step=$output/$index
  mkdir "$step"
  printf 'GPU_COHORT %s %d %s\n' "$mode" "$index" "$command"
  (
    # The outer action owns its deadline and complete cohort selection.
    # These raw binaries have no nested Bazel TestRunner.
    unset TEST_TIMEOUT TESTBRIDGE_TEST_ONLY TESTBRIDGE_TEST_RUNNER_FAIL_FAST
    unset TEST_TOTAL_SHARDS TEST_SHARD_INDEX TEST_SHARD_STATUS_FILE
    TEST_UNDECLARED_OUTPUTS_DIR="$step" XML_OUTPUT_FILE="$step/test.xml" "$command"
  )
  index=$((index + 1))
done
if [[ $mode == smoke ]]; then
  "$driver_tool" validate_checksum
fi
