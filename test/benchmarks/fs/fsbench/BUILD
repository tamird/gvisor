# Package fsbench provides utility functions for filesystem-related benchmarks.

load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//test/benchmarks/fs:__subpackages__"],
    licenses = ["notice"],
)

go_library(
    name = "fsbench",
    testonly = 1,
    srcs = ["fsbench.go"],
    data = ["//test/runner/fuse"],
    importpath = "gvisor.dev/gvisor/test/benchmarks/fs/fsbench",
    deps = [
        "//pkg/cleanup",
        "//pkg/test/dockerutil",
        "//test/benchmarks/harness",
        "//test/benchmarks/tools",
        "//test/metricsviz",
        "@com_github_docker_docker//api/types/container:go_default_library",
    ],
)

go_test(
    name = "fsbench_test",
    size = "small",
    srcs = ["fsbench_test.go"],
    args = ["-test.benchtime=1x"],
    # Invalidate cached Docker results when the configured runtime changes.
    data = ["//:release"],
    embed = [":fsbench"],
    # Requires Docker, the basic Alpine image, and the canonical FUSE server.
    local = True,
    tags = ["manual"],
    visibility = ["//:sandbox"],
    deps = ["//test/benchmarks/harness"],
)
