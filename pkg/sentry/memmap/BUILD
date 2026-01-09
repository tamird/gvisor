load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "memmap",
    srcs = [
        "mapping_set.go",
        "memmap.go",
    ],
    visibility = ["//pkg/sentry:internal"],
    deps = [
        "//pkg/context",
        "//pkg/errors/linuxerr",
        "//pkg/hostarch",
        "//pkg/log",
        "//pkg/safemem",
        "//pkg/segment",
        "//pkg/usermem",
    ],
)

go_test(
    name = "memmap_test",
    size = "small",
    srcs = ["mapping_set_test.go"],
    library = ":memmap",
    deps = ["//pkg/hostarch"],
)
