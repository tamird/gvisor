load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "test",
    srcs = [
        "test.go",
    ],
    deps = ["//pkg/state"],
)

go_test(
    name = "test_test",
    srcs = ["test_test.go"],
    library = ":test",
    deps = ["//pkg/state"],
)
