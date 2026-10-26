#!/bin/bash
# Copyright 2026 The gVisor Authors.
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

# Focused proof of the generic wrapper with the maintained cgroup-v1 case.
set -euo pipefail
test "$#" -eq 3
out=${TEST_UNDECLARED_OUTPUTS_DIR:?}
status=0
"$1" timeout --signal=TERM --kill-after=10s 180s "$2" \
  "--docker_test_config=$3" --runtime=runsc \
  -test.v -test.timeout=2m '-test.run=^TestCgroupV1$' \
  > "${out}/root-test.log" 2>&1 || status=$?
cat "${out}/root-test.log"
test "${status}" -eq 0
grep -E 'Private Docker .*cgroup=cgroupfs/1,' "${out}/root-test.log"
grep -F '=== RUN   TestCgroupV1' "${out}/root-test.log"
grep -E '^--- PASS: TestCgroupV1 \(' "${out}/root-test.log"
