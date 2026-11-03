#!/bin/bash
# Copyright 2026 The gVisor Authors.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# https://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
set -euo pipefail
inputs="${TEST_SRCDIR:?}/${TEST_WORKSPACE:?}/.github/qualification/unit-cgroup-v1/worker"
out="${TEST_UNDECLARED_OUTPUTS_DIR:?}"
unset CGROUPV2
source "$inputs/hook.bash"
[[ $CGROUPV2 == false && $(id -u) != 0 && $(uname -m) == x86_64 ]]
unshare -Ur true
printf '%s\n' "$CGROUPV2" "$(id -u)" "$(uname -r)" "$(uname -m)" > "$out/owner-mode"
if [[ -x "$inputs/selected-test" ]]; then
  sha256sum "$inputs/selected-test" | cut -d' ' -f1 > "$out/owner-elf.sha256"
  exec "$inputs/selected-test" "$@"
fi
