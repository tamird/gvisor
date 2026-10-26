load("@python_distribution_deps//:requirements.bzl", "requirement")
load("@rules_python//python/entry_points:py_console_script_binary.bzl", "py_console_script_binary")
load("//tools:defs.bzl", "bzl_library", "go_binary")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

exports_files(["requirements.txt"])

bzl_library(
    name = "defs_bzl",
    srcs = ["defs.bzl"],
    visibility = ["//:__subpackages__"],
    deps = ["@bazel_skylib//rules:common_settings"],
)

go_binary(
    name = "builder",
    srcs = ["main.go"],
    deps = ["@com_github_burntsushi_toml//:go_default_library"],
)

py_console_script_binary(
    name = "frontend",
    pkg = requirement("build"),
    script = "pyproject-build",
    deps = [
        requirement("setuptools"),
        requirement("wheel"),
    ],
)
