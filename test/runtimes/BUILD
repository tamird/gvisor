load("//test/docker:defs.bzl", "docker_daemon_config")
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

# Fork-only profile of the released concatenation body in one sandbox.
docker_daemon_config(
    name = "php_concat_profile_config",
    testonly = True,
    # The diagnostic parent materializes the prior source-built archive and
    # verifies SHA256 2960bf6f07cc9820e0c6be40edf22dfaeca62554121e67fd94504b43e75c2748.
    images = [":php_concat_profile_image.tar"],
    runtime_args = [
        "--platform=systrap",
        "--watchdog-action=panic",
        "--directfs=true",
        "--profile=true",
    ],
    tags = ["manual"],
)

go_test(
    name = "php_concat_profile_diagnostic",
    size = "large",
    timeout = "long",
    srcs = ["php_timing_test.go"],
    args = [
        "--docker_test_config=$(rootpath :php_concat_profile_config)",
        "-test.v",
    ],
    data = [":php_concat_profile_config"],
    exec_properties = docker_test_exec_properties(free_disk = "20GB"),
    rundir = ".",
    tags = ["manual"],
    target_compatible_with = ["@platforms//cpu:x86_64"],
    deps = ["//pkg/test/dockerutil"],
)
