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

# Fork-only comparison of the three failures from the full PHP 8.5.11 run.
# The native control uses the same source-built image and proctor.
docker_daemon_config(
    name = "php_upgrade_native_config",
    testonly = True,
    images = [":php8.5.11_image_amd64_tar"],
    runtime = None,
    tags = ["manual"],
)

[docker_command_test(
    name = "php_upgrade_" + mode + "_diagnostic",
    size = "large",
    timeout = "long",
    command = "//test/runtimes/runner",
    command_args = [
        "--lang=php",
        "--image=php8.5.11",
        "--batch=1",
        "--tests=" + ",".join([
            "ext/bcmath/tests/GHSA-x692-q9x7-8c3f.phpt",
            "ext/pdo_pgsql/tests/transations_deprecations.phpt",
            "Zend/tests/concat/concat_003.phpt",
        ]),
        "--runs_per_test=" + ("1" if mode == "native" else "5"),
        "--flaky_short_circuit=false",
    ],
    data = ["//test/runtimes/proctor"],
    docker_config = ":php_upgrade_native_config" if mode == "native" else ":php8.5.11_" + mode + "_docker_config",
    exec_properties = docker_test_exec_properties(free_disk = "20GB"),
    tags = ["manual"],
    target_compatible_with = ["@platforms//cpu:x86_64"],
) for mode in [
    "native",
    "directfs",
    "goferfs",
]]
