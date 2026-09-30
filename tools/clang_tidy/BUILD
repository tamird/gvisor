load("@rules_python//python:py_binary.bzl", "py_binary")
load("//tools:defs.bzl", "bzl_library")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//visibility:public"],
    licenses = ["notice"],
)

py_binary(
    name = "run",
    srcs = ["run.py"],
)

bzl_library(
    name = "clang_tidy_bzl",
    srcs = ["clang_tidy.bzl"],
    deps = [
        "//tools:cc_compile_inputs_bzl",
        "@rules_cc//cc/common",
    ],
)
