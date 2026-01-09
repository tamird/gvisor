load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "atomicptr",
    srcs = ["atomicptr.go"],
    visibility = ["//:sandbox"],
)

go_test(
    name = "atomicptr_test",
    size = "small",
    srcs = ["atomicptr_test.go"],
    library = ":atomicptr",
)
