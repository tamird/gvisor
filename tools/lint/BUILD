load("@bazel_skylib//rules:native_binary.bzl", "native_binary")
load("@lint_deps//:requirements.bzl", "requirement")
load("@rules_python//python/entry_points:py_console_script_binary.bzl", "py_console_script_binary")
load(":cc.bzl", "CPPLINT_FLAGS")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

exports_files(["requirements.txt"])

py_console_script_binary(
    name = "codespell",
    pkg = requirement("codespell"),
)

native_binary(
    name = "cpplint",
    src = "@cpplint",
    args = CPPLINT_FLAGS,
)

native_binary(
    name = "clang-format",
    src = "@llvm//tools:clang-format",
    out = "bin/clang-format",
)
