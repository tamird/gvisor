load("@aspect_rules_lint//format:defs.bzl", "format_multirun")
load("@rules_multirun//:defs.bzl", "command")
load("@rules_python//python:defs.bzl", "py_binary")
load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//tools:defs.bzl", "bzl_library")
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
    command = ":format",
    group = "go",
)

source_lint(
    name = "buildifier",
    command = ":format",
    group = "bazel",
)

fixer_test(
    name = "fixer_test",
    srcs = ["fixer_test.py"],
    args = [
        "$(rlocationpath :goimports_fix)",
        "$(rlocationpath :buildifier_fix)",
        "$(rlocationpath :format.check)",
        "$(rlocationpath :format)",
        "$(rlocationpath @analysis_sources//:files/pkg/tcpip/seqnum/seqnum.go.source)",
        "$(rlocationpath @analysis_sources//:files/tools/buildbuddy/BUILD.source)",
        "$(rlocationpath @analysis_sources//:files/MODULE.bazel.source)",
        "$(rlocationpath @analysis_sources//:files/tools/lint/defs.bzl.source)",
    ],
    data = [
        ":buildifier_fix",
        ":format",
        ":format.check",
        ":goimports_fix",
        "@analysis_sources//:files/MODULE.bazel.source",
        "@analysis_sources//:files/pkg/tcpip/seqnum/seqnum.go.source",
        "@analysis_sources//:files/tools/buildbuddy/BUILD.source",
        "@analysis_sources//:files/tools/lint/defs.bzl.source",
    ],
    main = "fixer_test.py",
    deps = ["@rules_python//python/runfiles"],
)

py_binary(
    name = "aspect_buildifier_adapter",
    srcs = ["aspect_buildifier.py"],
    main = "aspect_buildifier.py",
    deps = ["@rules_python//python/runfiles"],
)

command(
    name = "aspect_buildifier",
    arguments = ["$(rlocationpath @com_github_bazelbuild_buildtools//buildifier)"],
    command = ":aspect_buildifier_adapter",
    data = ["@com_github_bazelbuild_buildtools//buildifier"],
)

format_multirun(
    name = "format",
    disable_git_attribute_checks = True,
    go = "@org_golang_x_tools//cmd/goimports",
    go_check_args = [
        "-format-only",
        "-d",
    ],
    go_fix_args = [
        "-format-only",
        "-w",
        "-l",
    ],
    jobs = 1,
    starlark = ":aspect_buildifier",
    starlark_check_args = ["check"],
    starlark_fix_args = ["fix"],
    tags = ["manual"],
)

sh_test(
    name = "aspect_bash32_test",
    size = "small",
    timeout = "short",
    srcs = ["aspect_bash32_test.sh"],
    args = ["$(rlocationpath @aspect_rules_lint//format/private:format)"],
    data = ["@aspect_rules_lint//format/private:format"],
    tags = [
        "manual",
        "no-remote",
    ],
    target_compatible_with = ["@platforms//os:osx"],
)
