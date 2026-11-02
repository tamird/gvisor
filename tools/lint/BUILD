load("@bazel_lib//lib:run_binary.bzl", "run_binary")
load("@bazel_skylib//lib:shell.bzl", "shell")
load("@bazel_skylib//rules:native_binary.bzl", "native_binary")
load("@lint_inputs//:inputs.bzl", "INPUTS")
load("//tools:defs.bzl", "go_binary")
load(":defs.bzl", "python_inputs", "workspace_formatters")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//:__pkg__"],
    licenses = ["notice"],
)

exports_files([
    "buildifier_native.patch",
    "codespell.BUILD",
])

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

GOIMPORTS_TOOL = "@org_golang_x_tools//cmd/goimports"

# Formatting-only mode does not add or remove imports: generated packages may
# not exist until Bazel builds them.
GOIMPORTS_FLAGS = ["-format-only"]

# This golden output tests comment positions across merged import declarations.
# goimports would merge the declarations and move their comments.
GOIMPORTS_INPUTS = [p for p in INPUTS["go"] if p != "tools/go_generics/tests/simple/output.go"]

# The LLVM multicall driver selects clang-format from argv[0].
native_binary(
    name = "clang_format_tool",
    src = "@llvm//tools:clang-format",
    out = "bin/clang-format",
)

go_binary(
    name = "workspace_command",
    srcs = ["workspace_command.go"],
    pure = True,
    deps = ["@io_bazel_rules_go//go/runfiles"],
)

workspace_formatters(
    buildifier = dict(
        args = BUILDIFIER_FLAGS + [
            "--config=" + (".buildifier.json" if INPUTS["buildifier_config"] else "off"),
            "--mode=fix",
            "--lint=fix",
            "--",
        ] + INPUTS["buildifier"],
        data = [],
        tool = BUILDIFIER_TOOL,
    ),
    clang = dict(
        args = [
            "-i",
            "--",
        ] + ["./" + p for p in INPUTS["cc"]],
        data = ["@lint_inputs//:ROOT.d/.clang-format"],
        tool = ":clang_format_tool",
    ),
    goimports = dict(
        args = GOIMPORTS_FLAGS + [
            "-w",
            "-l",
            "--",
        ] + ["./" + p for p in GOIMPORTS_INPUTS],
        data = [],
        tool = GOIMPORTS_TOOL,
    ),
)

run_binary(
    name = "goimports_list",
    srcs = [
        "@lint_inputs//:ROOT",
        "@lint_inputs//:go",
    ],
    args = GOIMPORTS_FLAGS + [
        "-l",
        "--",
    ] + [shell.quote(ROOT + "/" + p.replace("$", "$$")) for p in GOIMPORTS_INPUTS],
    mnemonic = "GoimportsList",
    stdout = "goimports.list",
    tool = GOIMPORTS_TOOL,
)

# goimports -l reports filenames but exits successfully when formatting differs.
run_binary(
    name = "goimports",
    srcs = [":goimports.list"],
    args = [
        "-IB",
        "$(execpath :check_empty.py)",
        "$(execpath :goimports.list)",
    ],
    mnemonic = "LintReportCheck",
    progress_message = "Checking Go formatting; fix with bazel run //tools/lint:format_goimports",
    stdout = "goimports.report",
    tool = ":python_interpreter",
    tools = [
        ":check_empty.py",
        ":python_runtime",
    ],
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

run_binary(
    name = "clang_format",
    srcs = [
        "@lint_inputs//:ROOT",
        # Require the root style: without it clang-format silently uses LLVM style.
        "@lint_inputs//:ROOT.d/.clang-format",
        "@lint_inputs//:cc",
        "@lint_inputs//:clang_format_config",
    ],
    args = [
        "--dry-run",
        "--Werror",
        "--",
    ] + [shell.quote(ROOT + "/" + p.replace("$", "$$")) for p in INPUTS["cc"]],
    mnemonic = "ClangFormatCheck",
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

# Invoke the wheel with the registered interpreter. Its dictionaries must stay
# beside the Python package, where codespell opens them by filename.
run_binary(
    name = "spelling",
    srcs = [
        "//tools:.codespellrc",
        "@lint_inputs//:ROOT",
        "@lint_inputs//:cc",
        "@lint_inputs//:docs",
        "@lint_inputs//:go",
    ],
    outs = ["spelling.ok"],
    args = [
        "-IB",
        "$(execpath :spelling.py)",
        "$(execpath @codespell//:codespell_lib/__main__.py)",
        "$(execpath //tools:.codespellrc)",
        ROOT,
        "$(execpath :spelling.ok)",
    ] + [shell.quote(p.replace("$", "$$")) for kind in [
        "cc",
        "docs",
        "go",
    ] for p in INPUTS[kind]],
    mnemonic = "Codespell",
    tool = ":python_interpreter",
    tools = [
        ":python_runtime",
        ":spelling.py",
        "@codespell//:codespell_lib/__main__.py",
        "@codespell//:library",
    ],
)

filegroup(
    name = "check",
    srcs = [
        ":buildifier",
        ":clang_format",
        ":cpplint",
        ":goimports",
        ":spelling",
    ],
)
