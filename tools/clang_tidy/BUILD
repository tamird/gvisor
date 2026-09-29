load("@rules_python//python:py_binary.bzl", "py_binary")
load("//tools:defs.bzl", "bzl_library")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//visibility:public"],
    licenses = ["notice"],
)

exports_files(["tool.BUILD"])

py_binary(
    name = "run",
    srcs = ["run.py"],
)

config_setting(
    name = "linux_amd64",
    constraint_values = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
)

config_setting(
    name = "linux_arm64",
    constraint_values = [
        "@platforms//cpu:aarch64",
        "@platforms//os:linux",
    ],
)

alias(
    name = "clang_tidy",
    actual = select({
        ":linux_amd64": "@clang_tidy_linux_amd64//:clang_tidy/data/bin/clang-tidy",
        ":linux_arm64": "@clang_tidy_linux_arm64//:clang_tidy/data/bin/clang-tidy",
    }),
)

alias(
    name = "distribution",
    actual = select({
        ":linux_amd64": "@clang_tidy_linux_amd64//:distribution",
        ":linux_arm64": "@clang_tidy_linux_arm64//:distribution",
    }),
)

bzl_library(
    name = "clang_tidy_bzl",
    srcs = ["clang_tidy.bzl"],
    deps = [
        "//tools:cc_compile_inputs_bzl",
        "@rules_cc//cc/common",
    ],
)
