load("@com_google_protobuf//bazel:proto_library.bzl", "proto_library")
load("@io_bazel_rules_go//go:def.bzl", "go_library")
load("@io_bazel_rules_go//proto:def.bzl", "go_proto_library")
load("@rules_python//python:defs.bzl", "py_test")
load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//tools/bazeldefs:coverage_analysis_test.bzl", "coverage_selection_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

proto_library(
    name = "schema",
    testonly = True,
    srcs = ["probe.proto"],
)

# The upstream embed API keeps the two libraries' coverage selection separate.
go_library(
    name = "embedded",
    testonly = True,
    srcs = ["embedded.go"],
    embedsrcs = ["data.txt"],
    importpath = "gvisor.dev/gvisor/tools/bazeldefs/tests/coverage",
)

go_proto_library(
    name = "own",
    testonly = True,
    embed = [":embedded"],
    importpath = "gvisor.dev/gvisor/tools/bazeldefs/tests/coverage",
    protos = [":schema"],
    visibility = ["//tools/bazeldefs/tests/coverage/probe:__pkg__"],
)

coverage_selection_test(
    name = "selection_test",
    targets = [
        ":own",
        ":embedded",
    ],
)

filegroup(
    name = "inventory_sentry",
    srcs = select({
        "//tools:gotsan": ["//runsc/cmd/sentry:gvisor_sentry_race_coverage"],
        "//conditions:default": ["//runsc/cmd/sentry:gvisor_sentry_coverage"],
    }),
)

py_test(
    name = "inventory_test",
    size = "large",
    srcs = ["inventory_test.py"],
    args = ["$(rlocationpath :inventory_sentry)"],
    data = [":inventory_sentry"],
    stamp = -1,
    tags = ["manual"],
    deps = ["@rules_python//python/runfiles"],
)

sh_test(
    name = "legacy_tools_preflight_test",
    size = "small",
    timeout = "short",
    srcs = ["legacy_tools_preflight_test.sh"],
    args = [
        "$(rootpath @coverage_legacy_go_bootstrap//file)",
        "$(rootpath @coverage_legacy_go_source//file)",
        "$(rootpath //:go.mod)",
    ],
    data = [
        "//:go.mod",
        "@coverage_legacy_go_bootstrap//file",
        "@coverage_legacy_go_source//file",
    ],
    exec_compatible_with = ["//tools/bazeldefs:rbe_ubuntu24_c"],
    tags = [
        "manual",
        "no-local",
    ],
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
)
