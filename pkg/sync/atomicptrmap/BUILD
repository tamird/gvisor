load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//visibility:private"],
    licenses = ["notice"],
)

go_library(
    name = "atomicptrmap",
    stateify = False,
    srcs = [
        "atomicptrmap.go",
        "generic_atomicptrmap_unsafe.go",
    ],
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/gohacks",
        "//pkg/sync",
    ],
)

go_test(
    name = "atomicptrmap_test",
    size = "small",
    srcs = ["atomicptrmap_test.go"],
    library = ":atomicptrmap",
    deps = ["//pkg/sync"],
)
