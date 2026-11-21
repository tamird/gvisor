load("@bazel_lib//lib:run_binary.bzl", "run_binary")
load("@bazel_lib//lib:transitions.bzl", "platform_transition_binary")
load("@bazel_skylib//lib:shell.bzl", "shell")
load("@bazel_skylib//rules:native_binary.bzl", "native_binary")
load("@lint_inputs//:inputs.bzl", "CHECKOUT", "INPUTS")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

ROOT = "$(execpath @lint_inputs//:ROOT).d"

# LLVM dispatches its multicall binary by the basename in argv[0].
native_binary(
    name = "clang_format_tool",
    src = "@llvm//tools:clang-format",
    out = "bin/clang-format",
    # Without the root style clang-format silently falls back to LLVM style.
    data = ["@lint_inputs//:ROOT.d/.clang-format"],
)

platform_transition_binary(
    name = "format_clang",
    args = [
        "-i",
        "--",
    ] + [shell.quote((CHECKOUT + "/" + p).replace("$", "$$")) for p in INPUTS["cc"]],
    binary = ":clang_format_tool",
    target_platform = "@platforms//host",
)

run_binary(
    name = "clang_format",
    srcs = [
        "@lint_inputs//:ROOT",
        "@lint_inputs//:cc",
        "@lint_inputs//:clang_format_config",
    ],
    args = [
        "--dry-run",
        "--Werror",
        "--",
    ] + [shell.quote(ROOT + "/" + p.replace("$", "$$")) for p in INPUTS["cc"]],
    mnemonic = "ClangFormatCheck",
    progress_message = "Checking C and C++ formatting (fix with bazel run //tools/lint:format_clang)",
    stdout = "clang_format.report",
    tool = ":clang_format_tool",
)
