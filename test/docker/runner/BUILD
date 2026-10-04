load("//tools:defs.bzl", "go_binary")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//:sandbox"],
    licenses = ["notice"],
)

go_binary(
    name = "runner",
    testonly = True,
    srcs = ["main.go"],
    deps = ["//pkg/test/dockerutil"],
)
