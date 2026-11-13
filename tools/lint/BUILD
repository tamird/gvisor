load("@bazel_lib//lib:run_binary.bzl", "run_binary")
load("@bazel_lib//lib:transitions.bzl", "platform_transition_binary")
load("@bazel_skylib//lib:shell.bzl", "shell")
load("@bazel_skylib//rules:native_binary.bzl", "native_binary")
load("@lint_inputs//:inputs.bzl", "CHECKOUT", "INPUTS")
load(":defs.bzl", "buildifier_check", "buildifier_format", "python_inputs")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//:__pkg__"],
    licenses = ["notice"],
)

exports_files(["buildifier_native.patch"])

# ROOT.d holds only the exact source/config groups requested by this action.
ROOT = "$(execpath @lint_inputs//:ROOT).d"

# Native rule loads are excluded because gVisor doesn't use the suggested
# rules_shell and proto_library.bzl loads.
BUILDIFIER_FLAGS = [
    "--format=",
    "--warnings=-duplicated-name,-list-append,-native-py,-native-sh-binary,-native-sh-library,-native-sh-test,-native-proto",
]

# bazel run and run_binary expand Make variables, then split args into words.
# Double literal dollars before expansion and quote each filename so spaces
# and quotes survive as one argument.
buildifier_format(
    name = "format",
    args = BUILDIFIER_FLAGS + [
        shell.quote(("--config=" + CHECKOUT + "/.buildifier.json").replace("$", "$$")) if INPUTS["buildifier_config"] else "--config=off",
        "--mode=fix",
        "--lint=fix",
        "--",
    ] + [shell.quote((CHECKOUT + "/" + p).replace("$", "$$")) for p in INPUTS["buildifier"]],
)

buildifier_check(
    name = "buildifier",
    srcs = ["@lint_inputs//:buildifier"],
    config = "@lint_inputs//:buildifier_config" if INPUTS["buildifier_config"] else None,
    flags = BUILDIFIER_FLAGS,
)

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

python_inputs(
    name = "python_inputs",
    tool = "@cpplint",
)

[
    filegroup(
        name = "python_" + group,
        srcs = [":python_inputs"],
        output_group = group,
    )
    for group in [
        "interpreter",
        "main",
        "runtime",
    ]
]

run_binary(
    name = "cpplint",
    srcs = [
        "@lint_inputs//:ROOT",
        "@lint_inputs//:cc",
        "@lint_inputs//:cpplint_config",
    ],
    args = [
        "-IB",
        "$(execpath :python_main)",
        "--quiet",
        "--repository=" + ROOT,
        "--filter=-,+build/include_order,+build/c++11",
    ] + [shell.quote(ROOT + "/" + p.replace("$", "$$")) for p in INPUTS["cc"]],
    mnemonic = "Cpplint",
    stdout = "cpplint.report",
    tool = ":python_interpreter",
    tools = [
        ":python_main",
        ":python_runtime",
    ],
)

filegroup(
    name = "check",
    srcs = [
        ":buildifier",
        ":clang_format",
        ":cpplint",
    ],
)
