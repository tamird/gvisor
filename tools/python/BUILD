load("@python_typecheck_pip//:requirements.bzl", "requirement")
load("@rules_python//python:py_test.bzl", "py_test")
load("@rules_python//python/entry_points:py_console_script_binary.bzl", "py_console_script_binary")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

exports_files(["requirements_lock.txt"])

py_console_script_binary(
    name = "typecheck_test",
    binary_rule = py_test,
    pkg = requirement("mypy"),
    script = "mypy",
    size = "small",
    args = [
        "--config-file=$(rootpath :mypy.ini)",
        "--output=json",
        "$(rootpath //tools/buildkite:buildkite.py)",
        "$(rootpath //tools:compat/realpath.py)",
        "$(rootpath //tools:gen_compile_commands.py)",
    ],
    data = [
        ":mypy.ini",
        "//tools:compat/realpath.py",
        "//tools:gen_compile_commands.py",
        "//tools/buildkite:buildkite.py",
    ],
    target_compatible_with = ["@platforms//os:linux"],
)
