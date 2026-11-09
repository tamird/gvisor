load("@bazel_skylib//rules:common_settings.bzl", "string_flag")
load("@rules_python//python:py_library.bzl", "py_library")
load("@rules_python//python:py_test.bzl", "py_test")
load(":archives.bzl", "release_archives")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

py_library(
    name = "sandbox_py",
    srcs = [
        "gvisor/__init__.py",
        "gvisor/sandbox.py",
    ],
    imports = ["."],
    visibility = ["//:__subpackages__"],
)

exports_files(
    ["gvisor/sandbox.py"],
    visibility = ["//:__subpackages__"],
)

py_test(
    name = "sandbox_py_test",
    srcs = ["tests/sandbox_test.py"],
    data = ["//:release"],
    main = "tests/sandbox_test.py",
    tags = ["nogotsan"],
    deps = [
        ":sandbox_py",
    ],
)

string_flag(
    name = "dist_version",
    build_setting_default = "",
)

release_archives(
    name = "dist",
    srcs = [
        "README.md",
        ":sandbox_py",
    ],
    project = "pyproject.toml",
    tags = ["manual"],
)
