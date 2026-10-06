load("@lint_deps//:requirements.bzl", "requirement")
load("@rules_python//python:defs.bzl", "py_binary", "py_test")
load("@rules_python//python/entry_points:py_console_script_binary.bzl", "py_console_script_binary")
load("//tools:defs.bzl", "bzl_library")
load(":defs.bzl", "gofmt_tools", "source_lint")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//visibility:public"],
    licenses = ["notice"],
)

exports_files(["requirements.txt"])

bzl_library(
    name = "defs_bzl",
    srcs = ["defs.bzl"],
    deps = [
        "@platforms//host:constraints_lib",
        "@rules_python//python:defs_bzl",
        "@with_cfg.bzl//:with_cfg",
    ],
)

gofmt_tools(name = "gofmt")

py_console_script_binary(
    name = "codespell",
    pkg = requirement("codespell"),
)

py_binary(
    name = "cpplint_tool",
    srcs = ["@cpplint//:cpplint.py"],
    main = "@cpplint//:cpplint.py",
)

source_lint(
    name = "gofmt",
    fix_tool = ":gofmt_fix_tool",
    groups = ["go"],
    tool = ":gofmt_check_tool",
)

source_lint(
    name = "clang-format",
    configs = ["clang_configs"],
    fix_tool = "@llvm//tools:clang-format",
    groups = ["cc"],
    tool = "@llvm//tools:clang-format",
)

source_lint(
    name = "cpplint",
    configs = ["cpplint_configs"],
    groups = ["cc"],
    tool = ":cpplint_tool",
)

source_lint(
    name = "buildifier",
    fix_tool = "@com_github_bazelbuild_buildtools//buildifier",
    groups = ["bazel"],
    tool = "@com_github_bazelbuild_buildtools//buildifier",
)

source_lint(
    name = "spelling",
    configs = ["spelling_configs"],
    groups = [
        "docs",
        "go",
        "cc",
    ],
    tool = ":codespell",
)

test_suite(
    name = "lint_tests",
    tests = [
        ":buildifier",
        ":clang-format",
        ":cpplint",
        ":gofmt",
        ":spelling",
    ],
)

# Qualification-only owner; never included in the maintained lint proposal.
py_test(
    name = "formatter_parity_test",
    size = "medium",
    timeout = "long",
    srcs = ["formatter_parity.py"],
    args = [
        "--old=$(rlocationpath @lint_previous_clang_format//:clang-format)",
        "--new=$(rlocationpath @llvm//tools:clang-format)",
        "--sources=$(rlocationpath @analysis_sources//:cc.json)",
        "--configs=$(rlocationpath @analysis_sources//:clang_configs.json)",
    ],
    data = [
        "@analysis_sources//:cc.json",
        "@analysis_sources//:cc_files",
        "@analysis_sources//:clang_configs.json",
        "@analysis_sources//:clang_configs_files",
        "@lint_previous_clang_format//:clang-format",
        "@llvm//tools:clang-format",
    ],
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    main = "formatter_parity.py",
    tags = ["manual"],
    deps = ["@rules_python//python/runfiles"],
)
