load("@bazel_lib//lib:run_binary.bzl", "run_binary")
load("@bazel_skylib//lib:shell.bzl", "shell")
load("@buildifier_prebuilt//:buildtools.bzl", "buildtools")
load("@buildifier_prebuilt//:rules.bzl", "buildifier_binary")
load("@lint_inputs//:inputs.bzl", "INPUTS")
load(":defs.bzl", "python_inputs")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//:__pkg__"],
    licenses = ["notice"],
)

# Interactive CLI for fixing explicitly named files, using the upstream wrapper.
# Only the native checks below are intended to execute in a scratch worker.
buildifier_binary(name = "buildifier_cli")

# run_binary splits expanded args like a shell; quote each path separately.
# ROOT.d holds only the exact source/config groups requested by this action.
ROOT = "$(execpath @lint_inputs//:ROOT).d"

BUILDIFIER_FLAGS = [
    "--config=" + (ROOT + "/.buildifier.json" if INPUTS["buildifier_config"] else "off"),
    "--format=",
    "--mode=check",
    "--lint=warn",
    "--warnings=-duplicated-name,-list-append,-native-py,-native-sh-binary,-native-sh-library,-native-sh-test,-native-proto",
    "--",
]

# This selection is evaluated for the execution platform: run_binary's tool
# dependency is cfg="exec". Each public filegroup supplies the pinned, native
# executable instead of the supplier's shell-based launcher.
ASSETS = [a for a in buildtools.DEFAULT_ASSETS if a.name == "buildifier"]

[
    config_setting(
        name = buildtools.create_unique_name(a),
        constraint_values = [
            "@platforms//os:" + {"darwin": "osx"}.get(
                a.platform,
                a.platform,
            ),
            "@platforms//cpu:" + {
                "amd64": "x86_64",
                "arm64": "aarch64",
            }.get(
                a.arch,
                a.arch,
            ),
        ],
    )
    for a in ASSETS
]

alias(
    name = "buildifier_binary",
    actual = select({":" + buildtools.create_unique_name(a): "@" + buildtools.create_unique_name(a) + "//file" for a in ASSETS}),
)

# Qualification-only execution placement: compile the Go programs on an
# ordinary worker but run all three linters in the same empty image.
SCRATCH = {
    "Arch": "amd64",
    "OSFamily": "linux",
    "container-image": "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef",
    "workload-isolation-type": "oci",
}

run_binary(
    name = "buildifier",
    srcs = [
        "@lint_inputs//:ROOT",
        "@lint_inputs//:buildifier",
        "@lint_inputs//:buildifier_config",
    ],
    args = BUILDIFIER_FLAGS + [shell.quote(ROOT + "/" + p.replace("$", "$$")) for p in INPUTS["buildifier"]],
    exec_properties = SCRATCH,
    mnemonic = "BuildifierCheck",
    stdout = "buildifier.report",
    tool = ":buildifier_binary",
)

[
    run_binary(
        name = "buildifier_" + name,
        srcs = [
            "@lint_inputs//:ROOT",
            "@lint_inputs//:buildifier",
            "@lint_inputs//:buildifier_config",
        ],
        args = BUILDIFIER_FLAGS + [shell.quote(ROOT + "/" + p.replace("$", "$$")) for p in INPUTS["buildifier"]],
        exec_properties = SCRATCH,
        mnemonic = "Buildifier" + mnemonic,
        stdout = "buildifier_" + name + ".report",
        tool = tool,
    )
    for name, mnemonic, tool in [
        ("gazelle", "Gazelle", "@com_github_bazelbuild_buildtools//buildifier"),
        ("pinned", "Pinned", "@lint_buildtools_851//buildifier"),
    ]
]

[
    run_binary(
        name = "canonical_" + name,
        srcs = [
            "@lint_inputs//:ROOT",
            "@lint_inputs//:buildifier",
        ],
        args = [
            "--config=off",
            "--mode=print_if_changed",
            "--",
            ROOT + "/" + path,
        ],
        exec_properties = SCRATCH,
        mnemonic = "CanonicalBuildifier",
        stdout = "canonical_" + name + ".out",
        tool = ":buildifier_binary",
    )
    for name, path in [
        ("build", "tools/lint/BUILD"),
        ("module", "MODULE.bazel"),
    ]
]

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
