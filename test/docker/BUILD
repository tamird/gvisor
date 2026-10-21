load("@rules_pkg//pkg:mappings.bzl", "pkg_files")
load("//test/gpu:config.bzl", "GPU_SOURCE_IMAGES")
load("//tools:defs.bzl", "bzl_library", "go_binary", "pkg_tar")
load("//tools/bazeldefs:platforms.bzl", "docker_test_exec_properties")
load(":config.bzl", "CONTAINERD_IMAGES", "DOCKER_TESTS", "INTEGRATION_TESTS", "INTEGRATION_VARIANTS", "PLUGIN_NETWORK_VARIANT")
load(":defs.bzl", "SOURCE_ARCHIVE_EXEC_PROPERTIES", "configured_image_archive", "docker_command_test", "docker_daemon_config", "docker_image_archives", "image_source_command", "source_image_archive")

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

# ARM64 Docker production runs on the native Actions coordinator. Compilation
# and the daemon-free archive conversion retain their remote execution paths.
[
    source_image_archive(
        name = "gcsfuse_source_" + suffix,
        testonly = True,
        architecture = architecture,
        archive_exec_properties = SOURCE_ARCHIVE_EXEC_PROPERTIES,
        exec_compatible_with = [
            "@platforms//os:linux",
            "@platforms//cpu:" + architecture,
        ],
        exec_properties = docker_test_exec_properties(
            exec_group = "",
            free_disk = "40GB",
            memory = "8GB",
            start_daemon = True,
        ) if suffix == "amd64" else {},
        image = "fuse/gcs",
        remote = suffix == "amd64",
        tags = ["manual"],
    )
    for suffix, architecture in [
        ("amd64", "x86_64"),
        ("arm64", "aarch64"),
    ]
]

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

[
    configured_image_archive(
        name = "basic_" + image + "_source_" + architecture,
        testonly = True,
        actual = "//images/basic/" + image + ":image_archive",
        architecture = architecture,
        tags = ["manual"],
    )
    for image in [
        "alpine",
        "busybox",
        "tmpfile",
    ]
    for architecture in [
        "amd64",
        "arm64",
    ]
]

docker_image_archives(
    name = "images",
    extra_images = CONTAINERD_IMAGES,
    source_archives = {
        "basic/" + image: {
            architecture: ":basic_" + image + "_source_" + architecture
            for architecture in [
                "amd64",
                "arm64",
            ]
        }
        for image in [
            "alpine",
            "busybox",
            "tmpfile",
        ]
    } | {
        "basic/nginx": {"amd64": ":nginx_source_amd64"},
        "containerd/harness": {"amd64": ":containerd_harness_source_amd64"},
        "moby": {"amd64": ":moby_source_amd64"},
    },
)

source_image_archive(
    name = "nginx_source_amd64",
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
    image = "basic/nginx",
    tags = ["manual"],
)

# All containerd versions share this output instead of rebuilding the harness
# in each test daemon.
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

source_image_archive(
    name = "syzkaller_source_amd64",
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
    image = "syzkaller",
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
configured_image_archive(
    name = "gpu_ollama_client_source_amd64",
    testonly = True,
    actual = "//images/gpu/ollama/client:image_archive",
    tags = ["manual"],
)

configured_image_archive(
    name = "profile_helper_source_amd64",
    testonly = True,
    actual = "//images/benchmarks/profile-helper:image_archive",
    tags = ["manual"],
)

configured_image_archive(
    name = "gpu_ollama_bench_source_amd64",
    testonly = True,
    actual = "//images/gpu/ollama/bench:image_archive",
    tags = ["manual"],
)

configured_image_archive(
    name = "gpu_triton_source_amd64",
    testonly = True,
    actual = "//images/gpu/triton:image_archive",
    tags = ["manual"],
)

configured_image_archive(
    name = "gpu_ollama_small_source_amd64",
    testonly = True,
    actual = "//images/gpu/ollama/bench/small:image_archive",
    tags = ["manual"],
)

# GPU/TPU model images use the source/archive producer for CUDA images.
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
            # Producers retain the builder cache and two OCI layout copies.
            free_disk = {
                "gpu/ollama": "128GB",
                "gpu/ollama/singlemodel": "160GB",
                "gpu/stable-diffusion-xl": "256GB",
            }.get(image, "80GB"),
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
    name = "source_image_contracts_bzl",
    srcs = ["source_image_contracts.bzl"],
    deps = ["@rules_img//img:providers"],
)

bzl_library(
    name = "defs_bzl",
    srcs = ["defs.bzl"],
    deps = [
        ":config_bzl",
        ":source_image_contracts_bzl",
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

# Build Node once with its producer's memory budget. CPU shards load the
# declared archive through Make instead of compiling it in their smaller VMs.
# The producer still uses raw source_contexts, so this overlay is acyclic.
pkg_files(
    name = "nodejs_image_context",
    testonly = True,
    srcs = [":nodejs_source_amd64"],
    renames = {":nodejs_source_amd64": "images/runtimes/nodejs26.12.0/image.tar"},
    tags = ["manual"],
)

pkg_tar(
    name = "cpu_source_contexts_amd64",
    testonly = True,
    srcs = [":nodejs_image_context"],
    tags = ["manual"],
    deps = ["//images:cpu_source_contexts"],
)

image_source_command(
    name = "cpu_image_sources_amd64",
    testonly = True,
    architecture = "x86_64",
    contexts = ":cpu_source_contexts_amd64",
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
    name = "nodejs_cpu_image_load_amd64",
    testonly = True,
    architecture = "x86_64",
    contexts = ":cpu_source_contexts_amd64",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    images = ["runtimes_nodejs26.12.0"],
    tags = ["manual"],
)

image_source_command(
    name = "cpu_image_sources_smoke_amd64",
    testonly = True,
    architecture = "x86_64",
    contexts = "//images:cpu_source_contexts",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    images = [
        "basic_alpine",
        "benchmarks_profile-helper",
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
    tags = [
        "manual",
        "source-image-results",
    ],
    target_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
)

docker_command_test(
    name = "nodejs_cpu_image_load_amd64_test",
    size = "large",
    timeout = "eternal",
    architectures = ["amd64"],
    command = ":nodejs_cpu_image_load_amd64",
    docker_config = ":image_source_config",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    exec_properties = docker_test_exec_properties(
        free_disk = "40GB",
        memory = "8GB",
    ),
    shard_count = 1,
    tags = [
        "manual",
        "source-image-results",
    ],
    target_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
)

# Exercise Dockerfile rebuild and declared archive load in one shard.
docker_command_test(
    name = "cpu_image_sources_smoke_amd64_test",
    size = "large",
    timeout = "long",
    architectures = ["amd64"],
    command = ":cpu_image_sources_smoke_amd64",
    docker_config = ":image_source_config",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    exec_properties = docker_test_exec_properties(
        free_disk = "40GB",
        memory = "8GB",
    ),
    shard_count = 1,
    tags = [
        "manual",
        "source-image-results",
    ],
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
    tags = [
        "manual",
        "source-image-results",
    ],
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
    tags = [
        "manual",
        "source-image-results",
    ],
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
    tags = [
        "manual",
        "source-image-results",
    ],
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
    for variant in INTEGRATION_VARIANTS
    if variant.name
]

# ConnectToSelf belongs to the integration package, not the image suite.
test_suite(
    name = PLUGIN_NETWORK_VARIANT.name + "_tests",
    tags = ["manual"],
    tests = ["//test/e2e:integration_test_" + PLUGIN_NETWORK_VARIANT.name + "_owned"],
)

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

# Apply the Node test patches once for both filesystem modes.
source_image_archive(
    name = "nodejs_source_amd64",
    testonly = True,
    architecture = "x86_64",
    archive_exec_properties = SOURCE_ARCHIVE_EXEC_PROPERTIES,
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    exec_properties = docker_test_exec_properties(
        cpu = 8,
        exec_group = "",
        free_disk = "80GB",
        memory = "24GB",
        start_daemon = True,
    ),
    image = "runtimes/nodejs26.12.0",
    tags = ["manual"],
)

# Qualify the unchanged language recipes through Make's explicit source build.
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
        ),
        image = image,
        tags = ["manual"],
    )
    for name, image in [
        ("go_source_amd64", "runtimes/go1.22"),
        ("java_source_amd64", "runtimes/java21"),
        ("python_source_amd64", "runtimes/python3.12.3"),
    ]
]
