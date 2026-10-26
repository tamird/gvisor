load("//tools:defs.bzl", "go_binary")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_binary(
    name = "main",
    srcs = ["main.go"],
    visibility = ["//test/gpu:__pkg__"],
    deps = [
        "//pkg/log",
        "//pkg/sentry/devices/nvproxy",
        "//pkg/sentry/devices/nvproxy/nvconf",
        "//runsc/flag",
        "//tools/gpu/drivers",
        "@org_golang_x_sync//errgroup:go_default_library",
    ],
)

# The declared host action shares the public script's driver-selection loop.
filegroup(
    name = "all_drivers_script",
    srcs = ["all_drivers_test.sh"],
    visibility = ["//test/gpu:__pkg__"],
)
