load("@rules_python//python:py_binary.bzl", "py_binary")
load("//tools:defs.bzl", "bzl_library")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

exports_files(["requirements.txt"])

bzl_library(
    name = "defs_bzl",
    srcs = ["defs.bzl"],
    visibility = ["//:__subpackages__"],
    deps = [
        "@bazel_skylib//lib:paths",
        "@bazel_skylib//rules:common_settings",
        "@rules_python//python:py_executable_info",
    ],
)

py_binary(
    name = "frontend",
    srcs = ["dist.py"],
    main = "dist.py",
    deps = [
        "@python_distribution_deps//build:pkg",
        "@python_distribution_deps//setuptools:pkg",
        "@python_distribution_deps//tomli_w:pkg",
        "@python_distribution_deps//wheel:pkg",
    ],
)
