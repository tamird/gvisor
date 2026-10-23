load("//tools:defs.bzl", "go_library", "go_test")

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

go_test(
    name = "http_probe_cancellation_test",
    size = "small",
    srcs = ["http_probe_cancellation_test.go"],
    local = True,
    tags = ["manual"],
    deps = [
        ":harness",
        "//pkg/test/dockerutil",
        "//pkg/test/testutil",
    ],
)
