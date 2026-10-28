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

load("@bazel_lib//lib:copy_file.bzl", "copy_file")
load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//test/docker:defs.bzl", "docker_command_test", "docker_daemon_config")
load("//tools:defs.bzl", "go_test", "py_test")
load("//tools/bazeldefs:platforms.bzl", "docker_test_exec_properties")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

copy_file(
    name = "scratch_worker_copy",
    src = "README.md",
    out = "scratch-worker-readme",
)

py_test(
    name = "qualification_report_test",
    srcs = [
        "collect_actions.py",
        "qualification_report.py",
        "qualification_report_test.py",
        "unit_matrix.py",
    ],
    data = [
        "actions.sh",
        "license.bazelrc",
        "qualification.bazelrc",
        "qualify.sh",
        "//:github_workflows",
        "//test/benchmarks:lifecycle.json",
    ] + glob(["testdata/qualification_report/**"]),
    imports = ["."],
    main = "qualification_report_test.py",
    tags = ["manual"],
)

py_test(
    name = "unit_matrix_test",
    srcs = [
        "unit_matrix.py",
        "unit_matrix_test.py",
    ],
    data = ["qualify.sh"] + glob(["testdata/syscall_buckets/**"]),
    imports = ["."],
    main = "unit_matrix_test.py",
    tags = ["manual"],
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
    srcs = [
        "source_archive_common_test.go",
        "source_archive_test.go",
    ],
    rundir = ".",
    tags = ["manual"],
)

# Exercise the packaged binary without invoking the bucket-mutating entrypoint.
go_test(
    name = "gcsfuse_image_test",
    srcs = [
        "gcsfuse_image_test.go",
        "source_archive_common_test.go",
        "source_archive_test.go",
    ],
    rundir = ".",
    tags = ["manual"],
)

[
    docker_daemon_config(
        name = "gcsfuse_" + suffix + "_config",
        testonly = True,
        images = ["//test/docker:gcsfuse_source_" + suffix],
        runtime = None,
        tags = ["manual"],
    )
    for suffix in [
        "amd64",
        "arm64",
    ]
]

[
    docker_command_test(
        name = "gcsfuse_" + suffix + "_test",
        size = "medium",
        command = ":gcsfuse_image_test",
        command_args = [
            "-test.v",
            "--archive=$(rootpath //test/docker:gcsfuse_source_" + suffix + ")",
            "--image=fuse/gcs",
        ],
        data = ["//test/docker:gcsfuse_source_" + suffix],
        docker_config = ":gcsfuse_" + suffix + "_config",
        exec_compatible_with = [
            "@platforms//os:linux",
            "@platforms//cpu:" + architecture,
        ],
        exec_properties = docker_test_exec_properties(
            free_disk = "20GB",
            memory = "4GB",
        ),
        tags = [
            "external",
            "manual",
        ],
        target_compatible_with = [
            "@platforms//os:linux",
            "@platforms//cpu:" + architecture,
        ],
    )
    for suffix, architecture in [
        ("amd64", "x86_64"),
        ("arm64", "aarch64"),
    ]
]

# Measure and verify the same archive before allocating Docker's expanded image.
# Test actions stream the archive without extraction; compilation keeps its
# normal execution properties. The Triton owner is used after native assembly.
[
    go_test(
        name = name + "_archive_integrity_test",
        size = size,
        timeout = timeout,
        srcs = [
            "source_archive_common_test.go",
            "source_archive_integrity_test.go",
        ],
        architectures = ["amd64"],
        args = [
            "--archive=$(rootpath //test/docker:" + archive + ")",
            "--image=" + image,
        ],
        data = ["//test/docker:" + archive],
        exec_properties = select({
            "//tools/bazeldefs:rbe": {
                "test.EstimatedFreeDiskBytes": free_disk,
                "test.EstimatedMemory": "2GB",
                "test.workload-isolation-type": "oci",
            },
            "//conditions:default": {},
        }),
        rundir = ".",
        tags = [
            "manual",
            "no-local",
        ],
        target_compatible_with = ["@platforms//os:linux"],
    )
    for name, image, archive, free_disk, size, timeout in [
        ("source", "containerd/harness", "containerd_harness_source_amd64", "20GB", "medium", "moderate"),
        ("triton", "gpu/triton", "gpu_triton_source_amd64", "80GB", "enormous", "eternal"),
    ]
]

docker_daemon_config(
    name = "ollama_small_archive_config",
    testonly = True,
    images = ["//test/docker:gpu_ollama_small_source_amd64"],
    runtime = None,
    tags = ["manual"],
)

# One owned daemon loads the archive once for the generic identity check and
# the real server's list/show checks. No GPU or model inference is required.
go_test(
    name = "ollama_model_test",
    srcs = [
        "ollama_model_test.go",
        "source_archive_common_test.go",
        "source_archive_test.go",
    ],
    rundir = ".",
    tags = ["manual"],
)

docker_command_test(
    name = "ollama_small_archive_load_test",
    size = "enormous",
    timeout = "eternal",
    command = ":ollama_model_test",
    command_args = [
        "-test.v",
        "--archive=$(rootpath //test/docker:gpu_ollama_small_source_amd64)",
        "--image=gpu/ollama/bench/small",
        "--ollama-model=snowflake-arctic-embed2:568m-l-fp16",
        "--ollama-manifest=$(rootpath //images/gpu/ollama/bench:manifests/snowflake-arctic-embed2_568m-l-fp16.json)",
    ],
    data = [
        "//images/gpu/ollama/bench:manifests/snowflake-arctic-embed2_568m-l-fp16.json",
        "//test/docker:gpu_ollama_small_source_amd64",
    ],
    docker_config = ":ollama_small_archive_config",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    exec_properties = docker_test_exec_properties(
        free_disk = "80GB",
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
# Stable Diffusion/native Triton archives measure 21.7/48.5GB compressed.
# A historical Dockerfile-based load exhausted an 80GB worker. Ollama benchmark adds
# about 92.2GB of models, with compressed archive inputs and expanded Docker
# layers coexisting during load and verification.
_SOURCE_ARCHIVES = {
    "ollama": ("gpu/ollama", "gpu_ollama_source_amd64", "128GB"),
    "ollama_bench": ("gpu/ollama/bench", "gpu_ollama_bench_source_amd64", "512GB"),
    "ollama_singlemodel": ("gpu/ollama/singlemodel", "gpu_ollama_singlemodel_source_amd64", "160GB"),
    "stable_diffusion": ("gpu/stable-diffusion-xl", "gpu_stable_diffusion_source_amd64", "256GB"),
    "tpu_vllm": ("tpu/vllm", "tpu_vllm_source_amd64", "80GB"),
    "triton": ("gpu/triton", "gpu_triton_source_amd64", "256GB"),
}

[
    docker_daemon_config(
        name = name + "_archive_config",
        testonly = True,
        images = ["//test/docker:" + archive],
        runtime = None,
        tags = ["manual"],
    )
    for name, (_, archive, _) in _SOURCE_ARCHIVES.items()
]

[
    docker_command_test(
        name = name + "_archive_load_test",
        size = "enormous",
        timeout = "eternal",
        command = ":source_archive_test",
        command_args = [
            "-test.v",
            "--archive=$(rootpath //test/docker:" + archive + ")",
            "--image=" + image,
        ],
        data = ["//test/docker:" + archive],
        docker_config = ":" + name + "_archive_config",
        exec_compatible_with = [
            "@platforms//os:linux",
            "@platforms//cpu:x86_64",
        ],
        exec_properties = docker_test_exec_properties(
            free_disk = free_disk,
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
    for name, (image, archive, free_disk) in _SOURCE_ARCHIVES.items()
]

# The ordinary unit owners retain portable environment skips. Qualification
# explicitly selects these complementary identities to execute those cases.
[
    test_suite(
        name = "unit_environment_tests_" + architecture,
        tags = ["manual"],
        tests = [
            target + "_" + architecture
            for target in [
                "//pkg/pinring:pinring_afpacket_privileged_test",
                "//runsc/fsgofer:lisafs_hardlink_privileged_test",
                "//runsc/fsgofer:lisafs_permissions_nonroot_test",
                "//sandboxexec/sandbox:sandbox_network_nonroot_test",
            ]
        ],
    )
    for architecture in [
        "amd64",
        "arm64",
    ]
]
