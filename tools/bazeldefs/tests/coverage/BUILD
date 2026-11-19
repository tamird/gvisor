load("@com_google_protobuf//bazel:proto_library.bzl", "proto_library")
load("@io_bazel_rules_go//go:def.bzl", "go_library")
load("@io_bazel_rules_go//proto:def.bzl", "go_proto_library")
load("@rules_python//python:defs.bzl", "py_test")
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

py_test(
    name = "legacy_inventory_test",
    size = "large",
    srcs = ["legacy_inventory_test.py"],
    args = [
        "$(rlocationpath //runsc/cmd/sentry:gvisor_sentry_coverage)",
        "$(rlocationpath :testdata/legacy-1f33fe3003.txt.gz)",
    ],
    data = [
        "testdata/legacy-1f33fe3003.txt.gz",
        "//runsc/cmd/sentry:gvisor_sentry_coverage",
    ],
    tags = ["manual"],
    deps = ["@rules_python//python/runfiles"],
)
