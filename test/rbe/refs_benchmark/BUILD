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

load("@rules_shell//shell:sh_test.bzl", "sh_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

# The reviewed hosted method supplies these declared inputs from exact Bazel
# outputs. They are deliberately absent until that preparation has completed.
sh_test(
    name = "compare",
    size = "medium",
    srcs = ["compare.sh"],
    args = [
        "$(rootpath before.tar)",
        "$(rootpath after.tar)",
        "$(rootpath inputs.sha256)",
        "$(rootpath @org_golang_x_perf//cmd/benchstat)",
        "$(rootpath @llvm//tools:llvm-nm)",
        "$(rootpath @llvm//tools:llvm-objdump)",
    ],
    data = [
        "after.tar",
        "before.tar",
        "inputs.sha256",
        "@llvm//tools:llvm-nm",
        "@llvm//tools:llvm-objdump",
        "@org_golang_x_perf//cmd/benchstat",
    ],
    tags = [
        "manual",
        "no-local",
    ],
    target_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ] + select({
        "//tools/bazeldefs:rbe": [],
        "//conditions:default": ["@platforms//:incompatible"],
    }),
)
