load("//tools:defs.bzl", "bzl_library", "go_binary", "go_reset_target")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_binary(
    name = "go_generics",
    srcs = [
        "imports.go",
        "main.go",
        "remove.go",
    ],
    visibility = ["//:sandbox"],
    deps = ["//tools/go_generics/globals"],
)

go_reset_target(
    name = "go_generics_tool",
    dep = ":go_generics",
    visibility = ["//:sandbox"],
)

bzl_library(
    name = "defs_bzl",
    srcs = ["defs.bzl"],
    visibility = ["//visibility:private"],
)
