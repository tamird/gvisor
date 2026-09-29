load("@bazel_skylib//rules:common_settings.bzl", "string_flag")
load("@rules_python//python:py_library.bzl", "py_library")
load("@rules_python//python:py_test.bzl", "py_test")
load("//tools:defs.bzl", "namespace_test_exec_properties")
load("//tools/python_distribution:defs.bzl", "python_distribution")

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

string_flag(
    name = "release_version",
    build_setting_default = "",
)

python_distribution(
    name = "dist",
    srcs = ["README.md"] + glob(["gvisor/*.py"]),
    pyproject = "pyproject.toml",
    version = ":release_version",
)

py_test(
    name = "sandbox_py_test",
    srcs = ["tests/sandbox_test.py"],
    data = ["//:release"],
    # The binding starts the runtime, including namespaces and cgroups.
    exec_properties = namespace_test_exec_properties(),
    main = "tests/sandbox_test.py",
    tags = ["nogotsan"],
    deps = [
        ":sandbox_py",
    ],
)
