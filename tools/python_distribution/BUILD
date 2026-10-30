load("@python_distribution_deps//:requirements.bzl", "requirement")
load("@rules_python//python:py_binary.bzl", "py_binary")
load("//tools:defs.bzl", "bzl_library", "go_binary")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

exports_files(["requirements.txt", "setuptools_reproducible_sdist.patch"])

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

go_binary(
    name = "builder",
    srcs = ["main.go"],
    deps = ["@com_github_burntsushi_toml//:go_default_library"],
)

py_binary(
    name = "frontend",
    main_module = "build",
    deps = [
        requirement("build"),
        requirement("setuptools"),
        requirement("wheel"),
    ],
)
