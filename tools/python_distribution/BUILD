load("@bazel_skylib//rules:native_binary.bzl", "native_test")
load("@python_distribution_deps//:requirements.bzl", "requirement")
load("@rules_python//python:py_binary.bzl", "py_binary")
load("//tools:defs.bzl", "bzl_library")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

exports_files(["requirements.txt"])

native_test(
    name = "version_test",
    src = "version_test.sh",
    args = ["$(rootpath //tools:make_python_release.sh)"],
    data = ["//tools:make_python_release.sh"],
)

bzl_library(
    name = "defs_bzl",
    srcs = ["defs.bzl"],
    visibility = ["//:__subpackages__"],
    deps = ["@bazel_skylib//rules:common_settings"],
)

py_binary(
    name = "builder",
    srcs = ["builder.py"],
    visibility = ["//:__subpackages__"],
    deps = [
        requirement("build"),
        requirement("packaging"),
        requirement("setuptools"),
        requirement("wheel"),
    ],
)

py_binary(
    name = "verify",
    srcs = ["verify.py"],
    visibility = ["//:__subpackages__"],
    deps = [
        requirement("installer"),
        requirement("packaging"),
    ],
)
