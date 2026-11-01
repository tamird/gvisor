load("//tools:defs.bzl", "bzl_library", "go_binary", "go_reset_target")

package(default_applicable_licenses = ["//:license"])

licenses(["notice"])

go_binary(
    name = "go_marshal",
    srcs = ["main.go"],
    visibility = ["//:sandbox"],
    deps = [
        "//tools/go_marshal/gomarshal",
    ],
)

go_reset_target(
    name = "go_marshal_tool",
    dep = ":go_marshal",
    visibility = ["//:sandbox"],
)

config_setting(
    name = "marshal_config_verbose",
    values = {"define": "gomarshal=verbose"},
    visibility = ["//:sandbox"],
)

bzl_library(
    name = "defs_bzl",
    srcs = ["defs.bzl"],
    visibility = ["//visibility:private"],
)
