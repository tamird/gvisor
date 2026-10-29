load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "fdnotifier",
    srcs = [
        "fdnotifier.go",
        "poll_unsafe.go",
    ],
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/sync",
        "//pkg/waiter",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)


go_test(
    name = "fdnotifier_test",
    size = "small",
    srcs = ["fdnotifier_test.go"],
    deps = [
        ":fdnotifier",
        "//pkg/waiter",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)
