load("@rules_pkg//pkg:mappings.bzl", "pkg_files")
load("//test/gpu:config.bzl", "GPU_SOURCE_IMAGES")
load("//tools:defs.bzl", "bzl_library", "go_binary", "pkg_tar")
load("//tools/bazeldefs:platforms.bzl", "docker_exec_properties", "docker_test_exec_properties")
load(":config.bzl", "CONTAINERD_IMAGES", "DOCKER_TESTS", "INTEGRATION_TESTS", "INTEGRATION_VARIANTS", "PLUGIN_NETWORK_VARIANT")
load(":defs.bzl", "amd64_image_archive", "docker_command_test", "docker_daemon_config", "docker_image_archives", "image_source_command", "source_image_archive")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//:sandbox"],
    licenses = ["notice"],
)

filegroup(
    name = "buildx",
    srcs = select({
        "@platforms//cpu:x86_64": ["@buildx_linux_amd64//file"],
        "@platforms//cpu:aarch64": ["@buildx_linux_arm64//file"],
    }),
)

# Conversion only reads OCI content and writes a compressed Docker archive.
# Keep it on the ordinary executor used by the completed large-image exports.
SOURCE_ARCHIVE_EXEC_PROPERTIES = select({
    "//tools/bazeldefs:rbe": dict(
        docker_exec_properties(
            exec_group = "",
            free_disk = "80GB",
            memory = "12GB",
            vfs = True,
        ).items() + [("workload-isolation-type", "oci")],
    ),
    "//conditions:default": {},
})

# A context-only marker changes the canonical directory hash without changing
# the Redis Dockerfile. Its apt RUN exercises the source build on a cache miss.
pkg_files(
    name = "source_image_miss_marker",
    srcs = ["source_image_miss.txt"],
    renames = {"source_image_miss.txt": "images/basic/redis/.qualification-miss"},
    tags = ["manual"],
)

pkg_tar(
    name = "source_image_miss_context",
    srcs = [":source_image_miss_marker"],
    tags = ["manual"],
    deps = ["//images:source_contexts"],
)

source_image_archive(
    name = "redis_source_miss_amd64",
    testonly = True,
    architecture = "x86_64",
    archive_exec_properties = SOURCE_ARCHIVE_EXEC_PROPERTIES,
    contexts = ":source_image_miss_context",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    exec_properties = docker_test_exec_properties(
        exec_group = "",
        free_disk = "40GB",
        memory = "8GB",
        start_daemon = True,
    ),
    image = "basic/redis",
    tags = ["manual"],
)

# Qualification-only cache misses for the actual maintained large Dockerfiles.
# The marker is hashed by Make but is not copied by either Dockerfile.
_SOURCE_BUILD_QUALIFICATION = {
    name.replace("_source_amd64", "_source_miss_amd64"): GPU_SOURCE_IMAGES[name]
    for name in [
        "gpu_stable_diffusion_source_amd64",
        "gpu_triton_source_amd64",
    ]
}

[
    pkg_files(
        name = name + "_marker",
        srcs = ["large_source_image_miss.txt"],
        renames = {"large_source_image_miss.txt": "images/" + image + "/.qualification-miss"},
        tags = ["manual"],
    )
    for name, image in _SOURCE_BUILD_QUALIFICATION.items()
]

[
    pkg_tar(
        name = name + "_context",
        srcs = [":" + name + "_marker"],
        tags = ["manual"],
        deps = ["//images:source_contexts"],
    )
    for name in _SOURCE_BUILD_QUALIFICATION
]

[
    source_image_archive(
        name = name,
        testonly = True,
        architecture = "x86_64",
        archive_exec_properties = SOURCE_ARCHIVE_EXEC_PROPERTIES,
        contexts = ":" + name + "_context",
        exec_compatible_with = [
            "@platforms//os:linux",
            "@platforms//cpu:x86_64",
        ],
        exec_properties = docker_test_exec_properties(
            exec_group = "",
            free_disk = "256GB",
            memory = "12GB",
            start_daemon = True,
            vfs = True,
        ),
        image = image,
        tags = ["manual"],
    )
    for name, image in _SOURCE_BUILD_QUALIFICATION.items()
]

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
    archive_exec_properties = SOURCE_ARCHIVE_EXEC_PROPERTIES,
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    exec_properties = docker_test_exec_properties(
        exec_group = "",
        free_disk = "40GB",
        memory = "8GB",
        start_daemon = True,
    ),
    image = "containerd/harness",
    tags = ["manual"],
)

# Reuse the public image context, including its precompiled Moby test binary.
source_image_archive(
    name = "moby_source_amd64",
    testonly = True,
    architecture = "x86_64",
    archive_exec_properties = SOURCE_ARCHIVE_EXEC_PROPERTIES,
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    exec_properties = docker_test_exec_properties(
        exec_group = "",
        free_disk = "40GB",
        memory = "8GB",
        start_daemon = True,
    ),
    image = "moby",
    tags = ["manual"],
)

# Build the CUDA contexts with their declared Go runners without requiring GPU
# execution. The GPU smoke and CUDA cohorts consume these declared archives.
[
    source_image_archive(
        name = name,
        testonly = True,
        architecture = "x86_64",
        archive_exec_properties = SOURCE_ARCHIVE_EXEC_PROPERTIES,
        exec_compatible_with = [
            "@platforms//os:linux",
            "@platforms//cpu:x86_64",
        ],
        exec_properties = docker_test_exec_properties(
            exec_group = "",
            free_disk = "80GB",
            memory = "12GB",
            start_daemon = True,
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

# The Bazel-built client is one OCI layer. Preserve the AMD64 image label
# used by the public GPU cohorts independently of the caller's architecture.
amd64_image_archive(
    name = "gpu_ollama_client_source_amd64",
    testonly = True,
    srcs = ["//images/gpu/ollama/client:image_archive"],
    tags = ["manual"],
)

amd64_image_archive(
    name = "profile_helper_source_amd64",
    testonly = True,
    srcs = ["//images/benchmarks/profile-helper:image_archive"],
    tags = ["manual"],
)

# Other runtime GPU images use the source/archive producer for CUDA images.
# Source builds require a disposable namespace-capable worker; no GPU test runs.
[
    source_image_archive(
        name = name,
        testonly = True,
        architecture = "x86_64",
        archive_exec_properties = SOURCE_ARCHIVE_EXEC_PROPERTIES,
        exec_compatible_with = [
            "@platforms//os:linux",
            "@platforms//cpu:x86_64",
        ],
        exec_properties = docker_test_exec_properties(
            exec_group = "",
            # The measured Triton load used 119.5GB before a builder cache.
            # Large source builds also retain compressed content and snapshots.
            free_disk = "256GB" if image in [
                "gpu/stable-diffusion-xl",
                "gpu/triton",
            ] else "80GB",
            memory = "12GB",
            start_daemon = True,
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
        "@with_cfg.bzl//:with_cfg",
    ],
)

image_source_command(
    name = "cpu_image_sources_amd64",
    testonly = True,
    architecture = "x86_64",
    contexts = "//images:cpu_source_contexts",
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
    contexts = "//images:cpu_source_contexts",
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
    contexts = "//images:gpu_source_contexts",
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
    contexts = "//images:gpu_source_contexts",
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
    archive_exec_properties = SOURCE_ARCHIVE_EXEC_PROPERTIES,
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    exec_properties = docker_test_exec_properties(
        exec_group = "",
        free_disk = "40GB",
        memory = "8GB",
        start_daemon = True,
    ),
    image = "runtimes/php8.5.11",
    tags = ["manual"],
)
