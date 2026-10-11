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

set -euo pipefail
runfiles=${TEST_SRCDIR:?}/${TEST_WORKSPACE:?}
native=$runfiles/test/syscalls/linux/vdso_clock_gettime_test
identity=$runfiles/test/rbe/worker_identity
[[ -x $native && -x $identity ]]
out=${TEST_UNDECLARED_OUTPUTS_DIR:?}/native-handoff
mkdir -p "$out"
{
  cat /proc/cpuinfo
  cat /proc/self/status
  if [[ -r /sys/devices/system/clocksource/clocksource0/current_clocksource ]]; then
    cat /sys/devices/system/clocksource/clocksource0/current_clocksource
  fi
} > "$out/host.txt"

handoff() {
  local phase=$1 status=0
  # Each child owns its XML. Do not let its GTest bookkeeping overwrite the
  # original owner's shard, premature-exit or result files.
  timeout --signal=TERM --kill-after=5s 30s env \
    -u GTEST_SHARD_INDEX -u GTEST_TOTAL_SHARDS -u GTEST_SHARD_STATUS_FILE \
    -u TEST_SHARD_INDEX -u TEST_TOTAL_SHARDS -u TEST_SHARD_STATUS_FILE \
    -u TEST_PREMATURE_EXIT_FILE -u TESTBRIDGE_TEST_ONLY -u TEST_ON_GVISOR \
    XML_OUTPUT_FILE="$out/$phase.xml" \
    "$native" --gtest_also_run_disabled_tests \
    --gtest_filter=DISABLED_NativeCycleHandoffTest.SerializedCounters \
    "--gtest_output=xml:$out/$phase.xml" > "$out/$phase.log" 2>&1 || status=$?
  if ! printf '%s\n' "$status" > "$out/$phase-exit.txt"; then
    if (( status == 0 )); then status=1; fi
  fi
  return "$status"
}

before=0
handoff before || before=$?
original=0
"$identity" "$@" || original=$?
capture=0
printf '%s\n' "$original" > "$out/original-exit.txt" || capture=$?
after=0
handoff after || after=$?

# Preserve the original owner failure independently of control failures.
result=$original
if (( result == 0 )); then result=$before; fi
if (( result == 0 )); then result=$after; fi
if (( result == 0 )); then result=$capture; fi
exit "$result"
