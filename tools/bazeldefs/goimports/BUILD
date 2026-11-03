load("//tools:defs.bzl", "go_binary")

package(default_applicable_licenses = ["//:license"])

licenses(["notice"])

go_binary(
    name = "goimports",
    srcs = ["main.go"],
    visibility = ["//tools/bazeldefs:__pkg__"],
    deps = ["@org_golang_x_tools//imports"],
)
