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

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

# Infrastructure smoke coverage, not the complete unit or syscall test lanes.
test_suite(
    name = "smoke",
    tags = ["manual"],
    tests = [
        "//pkg/sync:sync_test",
        "//pkg/waiter:waiter_test",
        "//test/syscalls:clock_getres_test_native",
        "//test/syscalls:clock_getres_test_runsc_systrap_directfs",
        "//test/util:posix_error_test",
    ],
)
