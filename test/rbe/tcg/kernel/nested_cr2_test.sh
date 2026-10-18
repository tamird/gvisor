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

uname -a
# This diagnosis concerns nested SVM; a successful VMX run cannot exercise it.
grep -m1 '^vendor_id' /proc/cpuinfo
grep -m1 '^model name' /proc/cpuinfo
grep -qm1 -w svm /proc/cpuinfo
[[ -d /sys/module/kvm_amd ]]
nested=$(cat /sys/module/kvm_amd/parameters/nested)
printf 'kvm_amd nested=%s\n' "$nested"
[[ $nested == 1 || $nested == Y ]]
sudo -n -- "$1" | tee "${TEST_TMPDIR}/selftest.log"
# The upstream program returns success when nested virtualization is absent.
# Require actual page faults in both phases instead of accepting that skip.
grep -Eq '^Guest page faults: [1-9][0-9]*$' "${TEST_TMPDIR}/selftest.log"
grep -Eq '^Guest page faults \(in L2\): [1-9][0-9]*$' "${TEST_TMPDIR}/selftest.log"
