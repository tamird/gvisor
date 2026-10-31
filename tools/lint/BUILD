load("@bazel_lib//lib:run_binary.bzl", "run_binary")
load("@bazel_skylib//lib:shell.bzl", "shell")
load("@lint_inputs//:inputs.bzl", "INPUTS")
load("//tools:defs.bzl", "go_binary")
load(":defs.bzl", "python_inputs")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//:__pkg__"],
    licenses = ["notice"],
)

exports_files(["buildifier_native.patch"])

# run_binary splits expanded args like a shell; quote each path separately.
# ROOT.d holds only the exact source/config groups requested by this action.
ROOT = "$(execpath @lint_inputs//:ROOT).d"

BUILDIFIER_TOOL = "@buildifier_prebuilt//buildifier:native"

# Native rule loads are excluded because gVisor doesn't use the suggested
# rules_shell and proto_library.bzl loads.
BUILDIFIER_FLAGS = [
    "--format=",
    "--warnings=-duplicated-name,-list-append,-native-py,-native-sh-binary,-native-sh-library,-native-sh-test,-native-proto",
]

go_binary(
    name = "format",
    srcs = ["workspace_command.go"],
    args = ["$(rlocationpath %s)" % BUILDIFIER_TOOL] + BUILDIFIER_FLAGS + [
        "--config=" + (".buildifier.json" if INPUTS["buildifier_config"] else "off"),
        "--mode=fix",
        "--lint=fix",
        "--",
    ] + [shell.quote(p.replace("$", "$$")) for p in INPUTS["buildifier"]],
    data = [BUILDIFIER_TOOL],
    pure = True,
    deps = ["@io_bazel_rules_go//go/runfiles"],
)

run_binary(
    name = "buildifier",
    srcs = [
        "@lint_inputs//:ROOT",
        "@lint_inputs//:buildifier",
        "@lint_inputs//:buildifier_config",
    ],
    args = BUILDIFIER_FLAGS + [
        "--config=" + (ROOT + "/.buildifier.json" if INPUTS["buildifier_config"] else "off"),
        "--mode=check",
        "--lint=warn",
        "--",
    ] + [shell.quote(ROOT + "/" + p.replace("$", "$$")) for p in INPUTS["buildifier"]],
    mnemonic = "BuildifierCheck",
    stdout = "buildifier.report",
    tool = BUILDIFIER_TOOL,
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
        "@lint_inputs//:cpplint",
        "@lint_inputs//:cpplint_config",
    ],
    args = [
        "-IB",
        "$(execpath :python_main)",
        "--quiet",
        "--repository=" + ROOT,
        "--filter=-,+build/include_order,+build/c++11",
    ] + [shell.quote(ROOT + "/" + p.replace("$", "$$")) for p in INPUTS["cpplint"]],
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
        ":cpplint",
    ],
)
