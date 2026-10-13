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
load("//tools:defs.bzl", "go_test")
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

# Fork-only CPU check of the actual declared Ollama client image.
go_test(
    name = "ollama_client_test",
    srcs = ["ollama_client_test.go"],
    tags = ["manual"],
    deps = ["//pkg/test/dockerutil"],
)

go_test(
    name = "profile_helper_test",
    srcs = ["profile_helper_test.go"],
    tags = ["manual"],
    deps = ["//pkg/test/dockerutil"],
)

docker_daemon_config(
    name = "profile_helper_config",
    testonly = True,
    images = ["//test/docker:profile_helper_source_amd64"],
    runtime = None,
    tags = ["manual"],
)

docker_command_test(
    name = "profile_helper_image_test",
    size = "medium",
    command = ":profile_helper_test",
    command_args = ["-test.v"],
    docker_config = ":profile_helper_config",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    exec_properties = docker_test_exec_properties(free_disk = "20GB"),
    tags = [
        "external",
        "manual",
        "no-local",
    ],
    target_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
)

docker_daemon_config(
    name = "ollama_client_config",
    testonly = True,
    images = ["//test/docker:gpu_ollama_client_source_amd64"],
    runtime = None,
    tags = ["manual"],
)

docker_command_test(
    name = "ollama_client_image_test",
    size = "medium",
    command = ":ollama_client_test",
    command_args = ["-test.v"],
    docker_config = ":ollama_client_config",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    exec_properties = docker_test_exec_properties(free_disk = "20GB"),
    tags = [
        "external",
        "manual",
        "no-local",
    ],
    target_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
)

# CPU compatibility check of the actual source archive.
go_test(
    name = "source_archive_test",
    srcs = ["source_archive_test.go"],
    rundir = ".",
    tags = ["manual"],
)

docker_daemon_config(
    name = "source_archive_config",
    testonly = True,
    images = ["//test/docker:containerd_harness_source_amd64"],
    runtime = None,
    tags = ["manual"],
)

docker_command_test(
    name = "source_archive_load_test",
    size = "medium",
    command = ":source_archive_test",
    command_args = [
        "-test.v",
        "--archive=$(rootpath //test/docker:containerd_harness_source_amd64)",
    ],
    data = ["//test/docker:containerd_harness_source_amd64"],
    docker_config = ":source_archive_config",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    exec_properties = docker_test_exec_properties(free_disk = "20GB"),
    tags = [
        "external",
        "manual",
        "no-local",
    ],
    target_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
)

docker_daemon_config(
    name = "source_image_miss_config",
    testonly = True,
    images = ["//test/docker:redis_source_miss_amd64"],
    runtime = None,
    tags = ["manual"],
)

docker_command_test(
    name = "source_image_miss_load_test",
    size = "medium",
    command = ":source_archive_test",
    command_args = [
        "-test.v",
        "--archive=$(rootpath //test/docker:redis_source_miss_amd64)",
        "--image=basic/redis",
    ],
    data = ["//test/docker:redis_source_miss_amd64"],
    docker_config = ":source_image_miss_config",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    exec_properties = docker_test_exec_properties(free_disk = "20GB"),
    tags = [
        "external",
        "manual",
        "no-local",
    ],
    target_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
)

# The maintained Make path runs as the host UID with Docker socket access.
# Exercise the same builder helper as a non-root UID in a disposable VM.
sh_test(
    name = "source_image_docker_user_test",
    size = "large",
    srcs = ["source_image_docker_user.sh"],
    args = [
        "$(rootpath //test/docker:source_image_build.sh)",
        "$(rootpath //test/docker:buildx)",
        "$(rootpath @source_buildkit//:digest)",
        "$(rootpath @bsd_tar_toolchains//:resolved_target_toolchain)",
        "$(rootpath //test/docker:source_image_miss_context)",
    ],
    data = [
        "//test/docker:buildx",
        "//test/docker:source_image_build.sh",
        "//test/docker:source_image_miss_context",
        "@bsd_tar_toolchains//:resolved_target_toolchain",
        "@source_buildkit//:digest",
    ],
    env = {"LC_ALL": "C.UTF-8"},
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    exec_properties = docker_test_exec_properties(
        free_disk = "40GB",
        memory = "8GB",
        start_daemon = True,
    ),
    tags = [
        "external",
        "manual",
        "no-local",
    ],
    target_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
)

# Checks of the source exports. Each action owns a disposable daemon;
# no container or GPU workload is executed.
_LARGE_ARCHIVES = {
    "stable_diffusion": "gpu/stable-diffusion-xl",
    "triton": "gpu/triton",
}

[
    docker_daemon_config(
        name = name + "_archive_config",
        testonly = True,
        images = ["//test/docker:gpu_" + name + "_source_amd64"],
        runtime = None,
        tags = ["manual"],
    )
    for name in _LARGE_ARCHIVES
]

[
    docker_command_test(
        name = name + "_archive_load_test",
        size = "enormous",
        timeout = "eternal",
        command = ":source_archive_test",
        command_args = [
            "-test.v",
            "--archive=$(rootpath //test/docker:gpu_" + name + "_source_amd64)",
            "--image=" + image,
        ],
        data = ["//test/docker:gpu_" + name + "_source_amd64"],
        docker_config = ":" + name + "_archive_config",
        exec_compatible_with = [
            "@platforms//os:linux",
            "@platforms//cpu:x86_64",
        ],
        # The prior 80GB source worker ran out of disk while exporting Stable.
        # These archives are 21.7/48.3GB compressed; expanded sizes remain an
        # observed result, not an assumed equality with compressed bytes.
        exec_properties = docker_test_exec_properties(
            free_disk = "256GB",
            memory = "12GB",
            vfs = True,
        ),
        tags = [
            "external",
            "manual",
            "no-local",
        ],
        target_compatible_with = [
            "@platforms//os:linux",
            "@platforms//cpu:x86_64",
        ],
    )
    for name, image in _LARGE_ARCHIVES.items()
]
