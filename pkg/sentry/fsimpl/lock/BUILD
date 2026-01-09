load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "lock",
    srcs = [
        "lock.go",
        "lock_set_functions.go",
    ],
    visibility = ["//pkg/sentry:internal"],
    deps = [
        "//pkg/abi/linux",
        "//pkg/context",
        "//pkg/errors/linuxerr",
        "//pkg/log",
        "//pkg/segment",
        "//pkg/sync",
        "//pkg/waiter",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

go_test(
    name = "lock_test",
    size = "small",
    srcs = [
        "lock_range_test.go",
        "lock_test.go",
    ],
    library = ":lock",
    deps = [
        "//pkg/errors/linuxerr",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)
