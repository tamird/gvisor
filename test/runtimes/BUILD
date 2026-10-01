load("//test/docker:defs.bzl", "docker_command_test", "docker_daemon_config")
load("//test/runtimes:defs.bzl", "runtime_tests")
load("//tools:defs.bzl", "bzl_library", "more_shards", "most_shards")
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
    "php8.3.35": dict(
        exclude_file = "exclude/php8.3.35.csv",
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

# Validation only: consume the same declared archive without rebuilding PHP.
_PHP_METADATA_MODES = [
    ("native", None, []),
    (
        "directfs",
        "//:release",
        [
            "--platform=systrap",
            "--directfs=true",
        ],
    ),
    (
        "goferfs",
        "//:release",
        [
            "--platform=systrap",
            "--directfs=false",
        ],
    ),
]

[
    docker_daemon_config(
        name = "php_metadata_" + mode_name + "_config",
        testonly = True,
        images = ["//test/docker:php_source_amd64"],
        runtime = mode_runtime,
        runtime_args = mode_args,
        tags = ["manual"],
    )
    for mode_name, mode_runtime, mode_args in _PHP_METADATA_MODES
]

[
    docker_command_test(
        name = "php_metadata_" + mode_name + "_test",
        size = "large",
        timeout = "long",
        command = ":php_metadata.sh",
        command_args = [
            "$(rootpath //test/runtimes/proctor:proctor)",
            "runc" if mode_runtime == None else "runsc",
            mode_name,
        ],
        data = ["//test/runtimes/proctor"],
        docker_config = ":php_metadata_" + mode_name + "_config",
        exec_properties = docker_test_exec_properties(free_disk = "20GB"),
        tags = ["manual"],
        target_compatible_with = [
            "@platforms//os:linux",
            "@platforms//cpu:x86_64",
        ],
    )
    for mode_name, mode_runtime, mode_args in _PHP_METADATA_MODES
]
