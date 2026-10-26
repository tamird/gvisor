load("@lint_deps//:requirements.bzl", "requirement")
load("@rules_python//python:defs.bzl", "py_binary")
load("@rules_python//python/entry_points:py_console_script_binary.bzl", "py_console_script_binary")
load("//tools:defs.bzl", "bzl_library")
load(":defs.bzl", "source_lint")

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
        "//tools/bazeldefs:cgroup_test_bzl",
        "@rules_python//python:defs_bzl",
        "@with_cfg.bzl//:with_cfg",
    ],
)

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
    name = "goimports",
    fixable = True,
    groups = ["go"],
    tool = "@org_golang_x_tools//cmd/goimports",
)

source_lint(
    name = "clang-format",
    configs = ["clang_configs"],
    fixable = True,
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
    fixable = True,
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
        ":goimports",
        ":spelling",
    ],
)
