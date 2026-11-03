load("@io_bazel_rules_go//go:def.bzl", compile_go = "go_library")
load("//tools/nogo:defs.bzl", "nogo_test")
load(":archive.bzl", "go_library", "go_wrap_cc")

package(default_applicable_licenses = ["//:license"])

compile_go(
    name = "extra",
    srcs = ["extra.go"],
    importpath = "gvisor.dev/gvisor/tools/nogo/qualification_legacy/extra",
)

compile_go(
    name = "own",
    srcs = ["own.go"],
    importpath = "gvisor.dev/gvisor/tools/nogo/qualification_legacy/own",
    deps = [":extra"],
)

compile_go(
    name = "compiled_consumer",
    srcs = ["consumer.go"],
    importpath = "gvisor.dev/gvisor/tools/nogo/qualification_legacy/consumer",
    deps = [
        ":extra",
        ":own",
    ],
)

go_wrap_cc(
    name = "archive_only",
    compiled = ":own",
    extra = ":extra",
)

go_library(
    name = "consumer",
    srcs = ["consumer.go"],
    compiled = ":compiled_consumer",
    deps = [":archive_only"],
)

nogo_test(
    name = "legacy_test",
    config = "//:nogo_config",
    deps = [":consumer"],
)
