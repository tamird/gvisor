load("@buildifier_prebuilt//:rules.bzl", "buildifier_binary")
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

buildifier_binary(
    name = "buildifier_tool",
)

source_lint(
    name = "goimports",
    group = "go",
    tool = "@org_golang_x_tools//cmd/goimports",
)

source_lint(
    name = "buildifier",
    group = "bazel",
    tool = ":buildifier_tool",
)

fixer_test(
    name = "fixer_test",
    srcs = ["fixer_test.py"],
    args = [
        "$(rlocationpath :goimports_fix)",
        "$(rlocationpath @org_golang_x_tools//cmd/goimports)",
        "$(rlocationpath :buildifier_fix)",
        "$(rlocationpath :buildifier_tool)",
    ],
    data = [
        ":buildifier_fix",
        ":buildifier_tool",
        ":goimports_fix",
        "@org_golang_x_tools//cmd/goimports",
    ],
    main = "fixer_test.py",
    deps = ["@rules_python//python/runfiles"],
)
