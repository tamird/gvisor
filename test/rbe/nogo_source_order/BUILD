load("@io_bazel_rules_go//go:def.bzl", "go_source")
load("//tools:defs.bzl", "go_library", "go_test")

package(default_applicable_licenses = ["//:license"])

go_source(
    name = "first",
    srcs = ["first.go"],
)

go_source(
    name = "second",
    srcs = ["second.go"],
)

go_library(
    name = "source_order",
    srcs = ["own.go"],
    embed = [
        ":first",
        ":second",
    ],
    stateify = False,
)

go_test(
    name = "source_order_test",
    srcs = [
        "own.go",
        "own_test.go",
    ],
    embed = [
        ":first",
        ":second",
    ],
)
