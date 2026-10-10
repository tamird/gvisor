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
load("//test/docker:defs.bzl", "docker_command_test", "docker_daemon_config")
load("//tools/bazeldefs:platforms.bzl", "docker_test_exec_properties")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

# Fork-only observation of the guest used by each qualification test.
sh_binary(
    name = "worker_identity",
    testonly = True,
    srcs = ["worker_identity.sh"],
    tags = ["manual"],
    target_compatible_with = ["@platforms//os:linux"],
    visibility = ["//visibility:public"],
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

# Root tests return readable outputs to the unprivileged Actions Bazel server.
sh_binary(
    name = "local_root",
    testonly = True,
    srcs = ["local_root.sh"],
    tags = ["manual"],
    target_compatible_with = ["@platforms//os:linux"],
    visibility = ["//visibility:public"],
)

# Applied with --run_under before the selected test initializes its daemon.
sh_binary(
    name = "docker_setup",
    testonly = True,
    srcs = ["docker_setup.sh"],
    tags = ["manual"],
    target_compatible_with = ["@platforms//os:linux"],
    visibility = ["//visibility:public"],
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

docker_daemon_config(
    name = "systemd_fixture_docker_config",
    testonly = True,
    images = ["//test/docker:images_systemd_services_amd64_tar"],
    runtime = None,
    tags = ["manual"],
)

# The root suite and runtime share the native systemd manager namespace.
docker_command_test(
    name = "systemd_fixture_test",
    size = "large",
    command = "systemd_fixture.sh",
    command_args = [
        "$(rootpath //test/root:root_test)",
        "$(rootpath //test/docker:configure_runtime)",
        "$(rootpath //test/docker:images_basic_alpine_amd64_tar)",
        "$(rootpath //test/docker:images_basic_ubuntu_amd64_tar)",
    ],
    data = [
        "//:release",
        "//test/docker:configure_runtime",
        "//test/docker:images_basic_alpine_amd64_tar",
        "//test/docker:images_basic_ubuntu_amd64_tar",
        "//test/root:root_test",
    ],
    docker_config = ":systemd_fixture_docker_config",
    exec_properties = docker_test_exec_properties(
        free_disk = "30GB",
        memory = "4GB",
    ),
    tags = ["manual"],
    target_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
)
