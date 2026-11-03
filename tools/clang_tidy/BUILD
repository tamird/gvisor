load("//tools:defs.bzl", "bzl_library")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//visibility:public"],
    licenses = ["notice"],
)

bzl_library(
    name = "clang_tidy_bzl",
    srcs = ["clang_tidy.bzl"],
    deps = [
        "//tools:cc_compile_inputs_bzl",
        "@bazel_skylib//rules/directory:providers",
        "@rules_cc//cc/common",
    ],
)
