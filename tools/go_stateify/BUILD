load("//tools:defs.bzl", "bzl_library", "go_binary", "go_reset_target")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_binary(
    name = "stateify",
    srcs = ["main.go"],
    visibility = ["//:sandbox"],
    deps = [
        "//tools/constraintutil",
        "@org_golang_x_tools//imports",
    ],
)

go_reset_target(
    name = "stateify_tool",
    dep = ":stateify",
    visibility = ["//:sandbox"],
)

bzl_library(
    name = "defs_bzl",
    srcs = ["defs.bzl"],
    visibility = ["//visibility:private"],
)
