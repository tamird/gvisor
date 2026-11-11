load("//test/docker:defs.bzl", "docker_command_test", "docker_daemon_config", "docker_image_archive")
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
        source_archive = "//test/docker:go_source_amd64",
    ),
    "java21": dict(
        batch = 100,
        exclude_file = "exclude/java21.csv",
        ipv6 = True,
        lang = "java",
        native_reference = True,
        partitions = 40,
        proctor_privileged = False,
        proctor_user = "",
        shard_count = most_shards,
        source_archive = "//test/docker:java_source_amd64",
    ),
    "nodejs26.12.0": dict(
        exclude_file = "exclude/nodejs26.12.0.csv",
        lang = "nodejs",
        # The pinned large-Blob test holds a 2GiB buffer and a Blob; its
        # sandbox was OOM-killed with the default 4GB VM budget.
        memory = "8GB",
        partitions = 10,
        shard_count = most_shards,
        source_archive = "//test/docker:nodejs_source_amd64",
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
        source_archive = "//test/docker:python_source_amd64",
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

docker_image_archive(
    name = "debugger_node22",
    architecture = "amd64",
    image = "runtimes/nodejs22.2.0",
)

docker_daemon_config(
    name = "debugger_docker_config",
    testonly = True,
    images = [
        ":debugger_node22_tar",
        ":nodejs26.12.0_image_amd64_tar",
    ],
    runtime_args = ["--platform=systrap", "--directfs=true"],
)

docker_command_test(
    name = "node_debugger_pairs",
    size = "enormous",
    command = "node_debugger_pairs.py",
    command_args = ["$(rootpath //test/runtimes/proctor:proctor_bin)"],
    data = ["//test/runtimes/proctor:proctor_bin"],
    docker_config = ":debugger_docker_config",
    exec_properties = docker_test_exec_properties(
        free_disk = "80GB",
        memory = "8GB",
    ),
    tags = ["manual", "no-local"],
    target_compatible_with = ["@platforms//cpu:x86_64"],
)
