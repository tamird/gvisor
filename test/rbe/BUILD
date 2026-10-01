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
load("//test/docker:defs.bzl", "docker_daemon_config")
load("//tools/bazeldefs:platforms.bzl", "docker_test_exec_properties")

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

docker_daemon_config(
    name = "cgroup_v1_docker_config",
    testonly = True,
    images = ["//test/docker:images_basic_alpine_amd64_tar"],
    tags = ["manual"],
)

# Fork-only proof using the existing root test and daemon lifecycle.
sh_test(
    name = "cgroup_v1_fixture_test",
    size = "large",
    srcs = ["cgroup_v1_fixture.sh"],
    args = [
        "$(rootpath //test/root:root_test)",
        "$(rootpath :cgroup_v1_docker_config)",
    ],
    data = [
        ":cgroup_v1_docker_config",
        "//test/root:root_test",
    ],
    exec_properties = docker_test_exec_properties(free_disk = "20GB"),
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
