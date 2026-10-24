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

load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//tools/bazeldefs:platforms.bzl", "namespace_test_exec_properties")

package(default_applicable_licenses = ["//:license"])

sh_test(
    name = "probe_test",
    size = "small",
    srcs = ["probe.sh"],
    exec_properties = namespace_test_exec_properties(
        image = "docker://gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a",
    ),
    tags = ["manual"],
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
)
