load("@aspect_rules_lint//format:defs.bzl", "format_multirun", "format_test")
load("@bazel_lib//lib:copy_file.bzl", "copy_file")
load("@rules_python//python:defs.bzl", "py_binary")
load("//tools:defs.bzl", "bzl_library")
load(":aspect_preflight.bzl", "host_command")
load(":defs.bzl", "fixer_test", "source_lint")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

bzl_library(
    name = "defs_bzl",
    srcs = ["defs.bzl"],
    deps = [
        "@rules_python//python:defs_bzl",
        "@with_cfg.bzl//:with_cfg",
    ],
)

source_lint(
    name = "goimports",
    group = "go",
    tool = "@org_golang_x_tools//cmd/goimports",
)

source_lint(
    name = "buildifier",
    group = "bazel",
    tool = "@com_github_bazelbuild_buildtools//buildifier",
)

fixer_test(
    name = "fixer_test",
    srcs = ["fixer_test.py"],
    args = [
        "$(rlocationpath :goimports_fix)",
        "$(rlocationpath @org_golang_x_tools//cmd/goimports)",
        "$(rlocationpath :buildifier_fix)",
        "$(rlocationpath @com_github_bazelbuild_buildtools//buildifier)",
    ],
    data = [
        ":buildifier_fix",
        ":goimports_fix",
        "@com_github_bazelbuild_buildtools//buildifier",
        "@org_golang_x_tools//cmd/goimports",
    ],
    main = "fixer_test.py",
    deps = ["@rules_python//python/runfiles"],
)

py_binary(
    name = "aspect_buildifier_adapter",
    srcs = [
        "aspect_buildifier.py",
        "runner.py",
    ],
    main = "aspect_buildifier.py",
    deps = ["@rules_python//python/runfiles"],
)

host_command(
    name = "aspect_buildifier",
    arguments = ["$(rlocationpath @com_github_bazelbuild_buildtools//buildifier)"],
    command = ":aspect_buildifier_adapter",
    data = ["@com_github_bazelbuild_buildtools//buildifier"],
)

host_command(
    name = "aspect_goimports",
    command = "@org_golang_x_tools//cmd/goimports",
)

format_multirun(
    name = "aspect_format",
    disable_git_attribute_checks = True,
    go = ":aspect_goimports",
    go_check_args = ["-format-only", "-d"],
    go_fix_args = ["-format-only", "-w", "-l"],
    jobs = 1,
    starlark = ":aspect_buildifier",
    starlark_check_args = ["check"],
    starlark_fix_args = ["fix"],
    tags = ["manual"],
)

format_test(
    name = "aspect_format_preflight",
    srcs = [
        ":aspect_input_build",
        ":aspect_input_bzl",
        ":aspect_input_go",
        ":aspect_input_module",
    ],
    disable_git_attribute_checks = True,
    fix_target = ":aspect_format",
    go = ":aspect_goimports",
    go_check_args = ["-format-only", "-d"],
    starlark = ":aspect_buildifier",
    starlark_check_args = ["check"],
    tags = ["manual"],
)

fixer_test(
    name = "aspect_fixer_preflight_test",
    srcs = ["aspect_fixer_preflight_test.py"],
    args = [
        "$(rlocationpath :aspect_format.check)",
        "$(rlocationpath :aspect_format)",
        "$(rlocationpath :aspect_input_go)",
        "$(rlocationpath :aspect_input_build)",
        "$(rlocationpath :aspect_input_module)",
        "$(rlocationpath :aspect_input_bzl)",
    ],
    data = [
        ":aspect_format",
        ":aspect_format.check",
        ":aspect_input_build",
        ":aspect_input_bzl",
        ":aspect_input_go",
        ":aspect_input_module",
    ],
    main = "aspect_fixer_preflight_test.py",
    tags = ["manual"],
    deps = ["@rules_python//python/runfiles"],
)

# Generated output paths preserve formatter language and Buildifier file types
# without allowing mirrored source BUILD files to define external packages.
copy_file(
    name = "aspect_input_go",
    src = "@analysis_sources//:files/pkg/tcpip/seqnum/seqnum.go.source",
    out = "aspect_inputs/pkg/tcpip/seqnum/seqnum.go",
)

copy_file(
    name = "aspect_input_build",
    src = "@analysis_sources//:files/tools/buildbuddy/BUILD.source",
    out = "aspect_inputs/tools/buildbuddy/BUILD",
)

copy_file(
    name = "aspect_input_module",
    src = "@analysis_sources//:files/MODULE.bazel.source",
    out = "aspect_inputs/MODULE.bazel",
)

copy_file(
    name = "aspect_input_bzl",
    src = "@analysis_sources//:files/tools/lint/defs.bzl.source",
    out = "aspect_inputs/tools/lint/defs.bzl",
)
