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

load("//test/docker:defs.bzl", "docker_command_test", "docker_daemon_config")
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
    name = "systemd_fixture_docker_config",
    testonly = True,
    images = ["//test/docker:images_systemd_services_amd64_tar"],
    runtime = None,
    tags = ["manual"],
)

# Fork-only proof: the real host-manager tests run inside native systemd.
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
