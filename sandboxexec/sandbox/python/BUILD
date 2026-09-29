load("@bazel_skylib//rules:common_settings.bzl", "string_flag")
load("@bazel_skylib//rules:native_binary.bzl", "native_test")

# Placeholder: load py_library
# Placeholder: load py_test
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

filegroup(
    name = "distribution_sources",
    srcs = [
        "README.md",
        "pyproject.toml",
    ] + glob([
        "gvisor/*.py",
        "tests/*.py",
    ]),
)

python_distribution(
    name = "dist",
    srcs = [":distribution_sources"],
    pyproject = "pyproject.toml",
    version = ":release_version",
)

filegroup(
    name = "distribution_version",
    srcs = [":dist"],
    output_group = "version",
)

native_test(
    name = "distribution_test",
    src = "//tools/python_distribution:verify",
    args = [
        "--dist=$(rootpath :dist)",
        "--pyproject=$(rootpath pyproject.toml)",
        "--version-file=$(rootpath :distribution_version)",
        "$(rootpaths :distribution_sources)",
    ],
    data = [
        "pyproject.toml",
        ":dist",
        ":distribution_sources",
        ":distribution_version",
    ],
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
