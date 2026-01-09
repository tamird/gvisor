load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "ring",
    srcs = [
        "ring.go",
    ],
    stateify = False,
    visibility = ["//visibility:public"],
)

go_test(
    name = "ring_test",
    size = "small",
    srcs = [
        "ring_test.go",
    ],
    library = ":ring",
)
