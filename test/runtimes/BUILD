load("//test/runtimes:defs.bzl", "runtime_tests")
load("//tools:defs.bzl", "bzl_library", "go_test", "more_shards", "most_shards")
load("//tools/bazeldefs:platforms.bzl", "docker_test_exec_properties")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

# Partition counts match the language jobs in .buildkite/pipeline.yaml. The
# owned suites schedule those partitions in Bazel instead of separate CI jobs.
_RUNTIMES = {
    "go1.22": dict(
        exclude_file = "exclude/go1.22.csv",
        lang = "go",
        partitions = 10,
        shard_count = more_shards,
    ),
    "java21": dict(
        batch = 100,
        exclude_file = "exclude/java21.csv",
        lang = "java",
        partitions = 40,
        shard_count = most_shards,
    ),
    "nodejs22.2.0": dict(
        exclude_file = "exclude/nodejs22.2.0.csv",
        lang = "nodejs",
        # The pinned large-Blob test holds a 2GiB buffer and a Blob; its
        # sandbox was OOM-killed with the default 4GB VM budget.
        memory = "8GB",
        partitions = 10,
        shard_count = most_shards,
    ),
    "php8.5.11": dict(
        exclude_file = "exclude/php8.5.11.csv",
        lang = "php",
        partitions = 2,
        shard_count = more_shards,
        source_archive = "//test/docker:php_source_amd64",
    ),
    "python3.12.3": dict(
        exclude_file = "exclude/python.csv",
        lang = "python",
        partitions = 2,
        shard_count = more_shards,
    ),
}

runtime_tests(
    name = "tests",
    runtimes = _RUNTIMES,
)

bzl_library(
    name = "defs_bzl",
    srcs = ["defs.bzl"],
    visibility = ["//visibility:private"],
    deps = [
        "//test/docker:defs_bzl",
        "//tools:defs_bzl",
        "//tools/bazeldefs:platforms_bzl",
        "@bazel_skylib//lib:shell",
    ],
)

# Fork-only diagnostic: compare the same concatenation workload on one worker.
# A successful diagnostic does not qualify the upstream PHPT timing assertion.
go_test(
    name = "php_timing_diagnostic",
    size = "large",
    timeout = "long",
    srcs = ["php_timing_test.go"],
    args = [
        "--docker_test_config=$(rootpath :php8.5.11_directfs_docker_config)",
        "-test.v",
    ],
    data = [":php8.5.11_directfs_docker_config"],
    exec_properties = docker_test_exec_properties(free_disk = "20GB"),
    rundir = ".",
    tags = ["manual"],
    target_compatible_with = ["@platforms//cpu:x86_64"],
    deps = ["//pkg/test/dockerutil"],
)

# Fork-only check of one corrected PHPT's missing-extension result.
go_test(
    name = "php_extension_metadata_diagnostic",
    size = "large",
    timeout = "long",
    srcs = ["php_extension_metadata_test.go"],
    args = [
        "--docker_test_config=$(rootpath :php8.5.11_goferfs_docker_config)",
        "-test.v",
    ],
    data = [":php8.5.11_goferfs_docker_config"],
    exec_properties = docker_test_exec_properties(free_disk = "20GB"),
    rundir = ".",
    tags = ["manual"],
    target_compatible_with = ["@platforms//cpu:x86_64"],
    deps = ["//pkg/test/dockerutil"],
)
