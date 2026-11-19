load("@bazel_skylib//rules:native_binary.bzl", "native_test")
load("@python_typecheck_pip//:requirements.bzl", "requirement")
load("@rules_python//python:py_test.bzl", "py_test")
load("@rules_python//python/entry_points:py_console_script_binary.bzl", "py_console_script_binary")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

exports_files(["requirements_lock.txt"])

_SOURCES = [
    "//tools/buildkite:buildkite.py",
    "//tools:compat/realpath.py",
    "//tools:gen_compile_commands.py",
]
_SOURCE_ARGS = ["$(rootpath %s)" % source for source in _SOURCES]
_DATA = ["//:pyproject.toml"] + _SOURCES

py_console_script_binary(
    name = "mypy_test",
    size = "small",
    args = [
        "--config-file=$(rootpath //:pyproject.toml)",
        "--output=json",
    ] + _SOURCE_ARGS,
    binary_rule = py_test,
    data = _DATA,
    legacy_create_init = False,
    pkg = requirement("mypy"),
    script = "mypy",
    target_compatible_with = ["@platforms//os:linux"],
)

py_console_script_binary(
    name = "pyright_test",
    size = "small",
    args = [
        "--project=$(rootpath //:pyproject.toml)",
        "--outputjson",
        "--pythonpath=$(PYTHON3_ROOTPATH)",
    ] + _SOURCE_ARGS,
    binary_rule = py_test,
    data = _DATA + ["@rules_python//python:current_py_toolchain"],
    env = {
        "PYRIGHT_PYTHON_GLOBAL_NODE": "0",
        "PYRIGHT_PYTHON_NODEJS_WHEEL": "1",
        "PYRIGHT_PYTHON_USE_BUNDLED_PYRIGHT": "1",
    },
    legacy_create_init = False,
    pkg = requirement("pyright"),
    script = "pyright",
    target_compatible_with = ["@platforms//os:linux"],
    toolchains = ["@rules_python//python:current_py_toolchain"],
    deps = [requirement("nodejs-wheel-binaries")],
)

native_test(
    name = "ty_test",
    size = "small",
    src = "@python_typecheck_pip//ty:bin/ty",
    args = [
        "check",
        "--project=.",
        "--output-format=sarif",
    ] + _SOURCE_ARGS,
    data = _DATA,
    target_compatible_with = ["@platforms//os:linux"],
)
