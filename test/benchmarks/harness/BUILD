load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
load("//tools:defs.bzl", "go_library")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "harness",
    testonly = 1,
    srcs = [
        "harness.go",
        "machine.go",
        "util.go",
    ],
    importpath = "gvisor.dev/gvisor/test/benchmarks/harness",
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/cleanup",
        "//pkg/test/dockerutil",
        "//pkg/test/testutil",
        "@com_github_docker_docker//api/types/mount:go_default_library",
    ],
)

sh_binary(
    name = "coverage_comparison_host_docker",
    testonly = True,
    srcs = ["host_docker.sh"],
    data = [
        "//debian:gvisor-release-coverage-tar-zstd",
        "//images:benchmark_alpine_load",
        "//images/benchmarks/syscallbench:syscallbench_load",
        "//test/benchmarks/base:startup_test",
        "//test/benchmarks/base:syscallbench_test",
        "//tools/bazeldefs/tests/coverage:coverage_lifecycle_test",
        "//tools/benchmark/paired",
        "@coverage_baseline_release//:release.tar.zstd",
    ],
    tags = ["manual"],
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    visibility = ["//:sandbox"],
)
