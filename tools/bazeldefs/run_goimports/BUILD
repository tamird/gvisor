load("//tools:defs.bzl", "go_binary")

package(default_applicable_licenses = ["//:license"])

licenses(["notice"])

go_binary(
    name = "run_goimports",
    srcs = ["main.go"],
    visibility = ["//tools/bazeldefs:__pkg__"],
)
