load("//tools:defs.bzl", "go_binary")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_binary(
    name = "package_version",
    srcs = ["main.go"],
    pure = True,
    visibility = ["//tools/bazeldefs:__pkg__"],
)
