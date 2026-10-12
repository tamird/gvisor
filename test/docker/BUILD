load("@bazel_lib//lib:output_files.bzl", "output_files")
load("@rules_oci//oci:defs.bzl", "oci_load")
load("//test/gpu:config.bzl", "GPU_SOURCE_IMAGES")
load("//tools:defs.bzl", "bzl_library", "go_binary")
load("//tools/bazeldefs:platforms.bzl", "docker_exec_properties", "docker_test_exec_properties")
load(":config.bzl", "CONTAINERD_IMAGES", "DOCKER_TESTS", "INTEGRATION_TESTS", "INTEGRATION_VARIANTS", "PLUGIN_NETWORK_VARIANT")
load(":defs.bzl", "cached_source_layout", "docker_command_test", "docker_daemon_config", "docker_image_archives", "image_source_command", "source_image_archive")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//:sandbox"],
    licenses = ["notice"],
)

docker_image_archives(
    name = "images",
    extra_images = CONTAINERD_IMAGES,
    source_archives = {
        "containerd/harness": {"amd64": ":containerd_harness_source_amd64"},
        "moby": {"amd64": ":moby_source_amd64"},
    },
)

# All containerd versions share this output instead of rebuilding the harness
# in each test daemon. Other images and the ARM64 archive retain their pins.
source_image_archive(
    name = "containerd_harness_source_amd64",
    testonly = True,
    architecture = "x86_64",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    exec_properties = docker_test_exec_properties(
        exec_group = "",
        free_disk = "40GB",
        memory = "8GB",
    ),
    image = "containerd/harness",
    tags = ["manual"],
)

# Reuse the public image context, including its precompiled Moby test binary.
source_image_archive(
    name = "moby_source_amd64",
    testonly = True,
    architecture = "x86_64",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    exec_properties = docker_test_exec_properties(
        exec_group = "",
        free_disk = "40GB",
        memory = "8GB",
    ),
    image = "moby",
    tags = ["manual"],
)

# Build the CUDA contexts with their declared Go runners without requiring GPU
# execution. Runtime suites retain their independently pinned image archives.
[
    source_image_archive(
        name = name,
        testonly = True,
        architecture = "x86_64",
        exec_compatible_with = [
            "@platforms//os:linux",
            "@platforms//cpu:x86_64",
        ],
        exec_properties = docker_test_exec_properties(
            exec_group = "",
            free_disk = "80GB",
            memory = "12GB",
            vfs = True,
        ),
        image = image,
        tags = ["manual"],
    )
    for name, image in [
        ("cuda_samples_source_amd64", "gpu/cuda-tests"),
        ("cuda_samples_12_8_source_amd64", "gpu/cuda-tests-12-8"),
    ]
]

# Runtime GPU images use the same source/archive producer as the CUDA images.
# Building these archives requires Docker, but does not execute GPU tests.
[
    source_image_archive(
        name = name,
        testonly = True,
        architecture = "x86_64",
        exec_compatible_with = [
            "@platforms//os:linux",
            "@platforms//cpu:x86_64",
        ],
        exec_properties = docker_test_exec_properties(
            exec_group = "",
            free_disk = "80GB",
            memory = "12GB",
            vfs = True,
        ),
        image = image,
        tags = ["manual"],
    )
    for name, image in GPU_SOURCE_IMAGES.items()
]

bzl_library(
    name = "config_bzl",
    srcs = ["config.bzl"],
    visibility = ["//:sandbox"],
    deps = [
        "//external/tools/plugin-stack:defs_bzl",
        "//tools/bazeldefs:platforms_bzl",
    ],
)

bzl_library(
    name = "defs_bzl",
    srcs = ["defs.bzl"],
    deps = [
        ":config_bzl",
        "//tools:arch_bzl",
        "//tools:defs_bzl",
        "//tools/bazeldefs:platforms_bzl",
        "//tools/bazeldefs:test_architectures_bzl",
        "@bazel_lib//lib:output_files",
        "@bazel_skylib//lib:paths",
        "@bazel_skylib//lib:shell",
        "@rules_foreign_cc//toolchains/native_tools:tool_access",
        "@rules_oci//oci:defs",
    ],
)

image_source_command(
    name = "cpu_image_sources_amd64",
    testonly = True,
    architecture = "x86_64",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    tags = ["manual"],
)

image_source_command(
    name = "cpu_image_sources_arm64",
    testonly = True,
    architecture = "aarch64",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:aarch64",
    ],
    tags = ["manual"],
)

image_source_command(
    name = "gpu_image_sources_amd64",
    testonly = True,
    architecture = "x86_64",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    image_class = "gpu",
    tags = ["manual"],
)

image_source_command(
    name = "gpu_image_sources_arm64",
    testonly = True,
    architecture = "aarch64",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:aarch64",
    ],
    image_class = "gpu",
    tags = ["manual"],
)

docker_daemon_config(
    name = "image_source_config",
    testonly = True,
    runtime = None,
    tags = ["manual"],
)

docker_command_test(
    name = "cpu_image_sources_amd64_test",
    size = "large",
    timeout = "eternal",
    architectures = ["amd64"],
    command = ":cpu_image_sources_amd64",
    docker_config = ":image_source_config",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    exec_properties = docker_test_exec_properties(
        free_disk = "40GB",
        memory = "8GB",
    ),
    shard_count = 4,
    tags = ["manual"],
    target_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
)

# The existing pinned Docker image also declares Linux/ARM64. This preserves
# the public lane's two partitions; it does not claim worker qualification.
docker_command_test(
    name = "cpu_image_sources_arm64_test",
    size = "large",
    timeout = "eternal",
    architectures = ["arm64"],
    command = ":cpu_image_sources_arm64",
    docker_config = ":image_source_config",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:aarch64",
    ],
    exec_properties = docker_test_exec_properties(
        free_disk = "40GB",
        memory = "8GB",
    ),
    shard_count = 2,
    tags = ["manual"],
    target_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:aarch64",
    ],
)

docker_command_test(
    name = "gpu_image_sources_amd64_test",
    size = "large",
    timeout = "eternal",
    architectures = ["amd64"],
    command = ":gpu_image_sources_amd64",
    docker_config = ":image_source_config",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    exec_properties = docker_test_exec_properties(
        free_disk = "40GB",
        memory = "8GB",
    ),
    shard_count = 4,
    tags = ["manual"],
    target_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
)

docker_command_test(
    name = "gpu_image_sources_arm64_test",
    size = "large",
    timeout = "eternal",
    architectures = ["arm64"],
    command = ":gpu_image_sources_arm64",
    docker_config = ":image_source_config",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:aarch64",
    ],
    exec_properties = docker_test_exec_properties(
        free_disk = "40GB",
        memory = "8GB",
    ),
    shard_count = 2,
    tags = ["manual"],
    target_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:aarch64",
    ],
)

go_binary(
    name = "configure_runtime",
    testonly = True,
    srcs = ["configure_runtime.go"],
    deps = ["//pkg/test/dockerutil"],
)

# Explicit lists include the existing manual tests without selecting other
# Docker-dependent tests or generating one target per runtime configuration.
test_suite(
    name = "installed_tests",
    tags = ["manual"],
    tests = DOCKER_TESTS,
)

test_suite(
    name = "owned_tests",
    tags = ["manual"],
    tests = [target + "_owned" for target in DOCKER_TESTS],
)

[
    test_suite(
        name = variant.name + "_tests",
        tags = ["manual"],
        tests = [target + "_" + variant.name + "_owned" for target in INTEGRATION_TESTS],
    )
    for variant in INTEGRATION_VARIANTS + [PLUGIN_NETWORK_VARIANT]
    if variant.name
]

# Build PHP once and share its declared archive among the runtime owners.
source_image_archive(
    name = "php_source_amd64",
    testonly = True,
    architecture = "x86_64",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    exec_properties = docker_test_exec_properties(
        exec_group = "",
        free_disk = "40GB",
        memory = "8GB",
    ),
    image = "runtimes/php8.5.11",
    tags = ["manual"],
)

# Fork-only proof that the stock OCI loader converts a cached source image.
cached_source_layout(
    name = "oci_conversion_small_layout",
    testonly = True,
    architecture = "x86_64",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    exec_properties = docker_test_exec_properties(
        exec_group = "",
        free_disk = "40GB",
        memory = "8GB",
    ),
    image = "containerd/harness",
    tags = ["manual"],
)

filegroup(
    name = "oci_conversion_small_tags",
    testonly = True,
    srcs = [":oci_conversion_small_layout"],
    output_group = "repo_tags",
    tags = ["manual"],
)

oci_load(
    name = "oci_conversion_small_load",
    testonly = True,
    format = "docker",
    image = ":oci_conversion_small_layout",
    repo_tags = ":oci_conversion_small_tags",
    tags = ["manual"],
)

output_files(
    name = "oci_conversion_small_archive",
    testonly = True,
    output_group = "tarball",
    paths = ["test/docker/oci_conversion_small_load/tarball.tar"],
    target = ":oci_conversion_small_load",
    tags = ["manual"],
)

# Fork-only completion of the two cached image conversions. The maintained
# source_image_archive declarations above retain Make's cache-miss rebuilds.
LARGE_CACHED_IMAGES = {
    "gpu_stable_diffusion_cached_amd64": GPU_SOURCE_IMAGES["gpu_stable_diffusion_source_amd64"],
    "gpu_triton_cached_amd64": GPU_SOURCE_IMAGES["gpu_triton_source_amd64"],
}

[
    cached_source_layout(
        name = name + "_layout",
        testonly = True,
        architecture = "x86_64",
        exec_compatible_with = [
            "@platforms//os:linux",
            "@platforms//cpu:x86_64",
        ],
        # Fork-only worker comparison; retain the original resource requests.
        exec_properties = select({
            "//tools/bazeldefs:rbe": dict(
                docker_exec_properties(
                    exec_group = "",
                    free_disk = "80GB",
                    memory = "12GB",
                    vfs = True,
                ).items() + [("workload-isolation-type", "oci")],
            ),
            "//conditions:default": {},
        }),
        image = image,
        tags = ["manual"],
    )
    for name, image in LARGE_CACHED_IMAGES.items()
]

[
    filegroup(
        name = name + "_tags",
        testonly = True,
        srcs = [":" + name + "_layout"],
        output_group = "repo_tags",
        tags = ["manual"],
    )
    for name in LARGE_CACHED_IMAGES
]

[
    oci_load(
        name = name + "_load",
        testonly = True,
        exec_compatible_with = [
            "@platforms//os:linux",
            "@platforms//cpu:x86_64",
        ],
        exec_properties = docker_test_exec_properties(
            exec_group = "",
            free_disk = "80GB",
            memory = "12GB",
            vfs = True,
        ),
        format = "docker",
        image = ":" + name + "_layout",
        repo_tags = ":" + name + "_tags",
        tags = ["manual"],
    )
    for name in LARGE_CACHED_IMAGES
]

[
    output_files(
        name = name,
        testonly = True,
        output_group = "tarball",
        paths = ["test/docker/" + name + "_load/tarball.tar"],
        target = ":" + name + "_load",
        tags = ["manual"],
    )
    for name in LARGE_CACHED_IMAGES
]
