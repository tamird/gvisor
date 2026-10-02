load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "refs",
    srcs = [
        "refcounter.go",
        "refs_map.go",
        "refs_unsafe.go",
    ],
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/atomicbitops",
        "//pkg/context",
        "//pkg/log",
        "//pkg/sync",
    ],
)

go_test(
    name = "refs_test",
    size = "small",
    srcs = ["refs_test.go"],
    library = ":refs",
)
