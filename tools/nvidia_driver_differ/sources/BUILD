load("//tools:defs.bzl", "go_binary", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_binary(
    name = "update",
    srcs = ["update.go"],
    deps = [
        "//pkg/sentry/devices/nvproxy",
        "//tools/nvidia_driver_differ/parser",
    ],
)

go_test(
    name = "update_test",
    size = "small",
    srcs = [
        "update.go",
        "update_test.go",
    ],
    deps = [
        "//pkg/sentry/devices/nvproxy",
        "//tools/nvidia_driver_differ/parser",
    ],
)
