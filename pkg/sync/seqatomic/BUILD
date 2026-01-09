load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "seqatomic",
    srcs = ["generic_seqatomic_unsafe.go"],
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/gohacks",
        "//pkg/sync",
    ],
)

go_test(
    name = "seqatomic_test",
    size = "small",
    srcs = ["seqatomic_test.go"],
    library = ":seqatomic",
    deps = ["//pkg/sync"],
)
