load("//tools:defs.bzl", "go_binary")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_binary(
    name = "extract",
    srcs = ["main.go"],
    visibility = ["//tools/nvidia_driver_differ:__pkg__"],
    deps = [
        "//pkg/sentry/devices/nvproxy",
        "//pkg/sentry/devices/nvproxy/nvconf",
        "//tools/nvidia_driver_differ/parser",
    ],
)
