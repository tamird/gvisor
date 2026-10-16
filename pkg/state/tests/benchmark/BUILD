load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

# Common measurement-only fixture; both arms use this exact source set.
go_library(
    name = "benchmark",
    testonly = True,
    srcs = ["bench.go"],
    deps = ["//pkg/state"],
)

go_test(
    name = "benchmark_test",
    size = "small",
    srcs = ["bench_test.go"],
    embed = [":benchmark"],
    deps = [
        "//pkg/state",
        "//pkg/tcpip",
    ],
)
