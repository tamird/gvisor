load("//tools:defs.bzl", "bzl_library")
load(":defs.bzl", "source_lint")

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
