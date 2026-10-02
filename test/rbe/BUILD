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

load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
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

# Applied with --run_under before the selected test initializes its daemon.
sh_binary(
    name = "cgroup_v1",
    testonly = True,
    srcs = ["cgroup_v1.sh"],
    tags = ["manual"],
    target_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ] + select({
        "//tools/bazeldefs:rbe": [],
        "//conditions:default": ["@platforms//:incompatible"],
    }),
    visibility = ["//visibility:public"],
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
        "$(rootpath :cgroup_v1)",
        "$(rootpath //test/root:root_test)",
        "$(rootpath :cgroup_v1_docker_config)",
    ],
    data = [
        ":cgroup_v1",
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

# One invocation retains four distinct configured native test owners.
test_suite(
    name = "platform_matrix",
    tags = ["manual"],
    tests = [
        "//pkg/atomicbitops:atomicbitops_test_amd64",
        "//pkg/atomicbitops:atomicbitops_test_arm64",
        "//test/util:posix_error_test_amd64",
        "//test/util:posix_error_test_arm64",
    ],
)

# Fork-only observation of the existing native PTY input assertion.
sh_test(
    name = "pty_input_trace",
    size = "medium",
    srcs = ["pty_input_trace.sh"],
    args = ["$(rootpath //test/syscalls:pty_test_native)"],
    data = ["//test/syscalls:pty_test_native"],
    exec_properties = {
        "test.dockerUser": "root",
        "test.network-enable-ipv6": "true",
        "test.recycle-runner": "false",
        "test.workload-isolation-type": "firecracker",
    },
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

# Fork-only inspection of dynamic-probe access; no PTY case or probes run.
sh_test(
    name = "pty_probe_setup",
    size = "medium",
    srcs = ["pty_probe_setup.sh"],
    exec_properties = {
        "test.dockerUser": "root",
        "test.network-enable-ipv6": "true",
        "test.recycle-runner": "false",
        "test.workload-isolation-type": "firecracker",
    },
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

# Fork-only provider check; registered events remain disabled.
sh_test(
    name = "pty_probe_registration",
    size = "medium",
    srcs = ["pty_probe_registration.sh"],
    exec_properties = {
        "test.dockerUser": "root",
        "test.network-enable-ipv6": "true",
        "test.recycle-runner": "false",
        "test.workload-isolation-type": "firecracker",
    },
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
