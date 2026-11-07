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
        "//pkg/sentry/devices/nvproxy/nvconf",
        "@com_github_google_go_github_v84//github:go_default_library",
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
        "//pkg/sentry/devices/nvproxy/nvconf",
        "@com_github_google_go_github_v84//github:go_default_library",
    ],
)
