load("@io_bazel_rules_go//go:def.bzl", "go_library", "go_source")
load("//tools/nogo:defs.bzl", "nogo_test")

package(default_applicable_licenses = ["//:license"])

go_source(
    name = "embedded",
    srcs = ["embedded.go"],
    deps = [":dependency"],
)

go_source(
    name = "nested",
    srcs = ["nested.go"],
    embed = [":embedded"],
)

go_library(
    name = "dependency",
    srcs = ["dependency/value.go"],
    importpath = "gvisor.dev/gvisor/test/rbe/nogo_embed_deps/dependency",
)

go_library(
    name = "direct",
    srcs = ["direct.go"],
    embed = [":embedded"],
    importpath = "gvisor.dev/gvisor/test/rbe/nogo_embed_deps/direct",
)

go_library(
    name = "compiled",
    srcs = ["compiled.go"],
    importpath = "gvisor.dev/gvisor/test/rbe/nogo_embed_deps/mixed",
)

go_library(
    name = "mixed",
    srcs = ["mixed.go"],
    embed = [
        ":compiled",
        ":nested",
    ],
    importpath = "gvisor.dev/gvisor/test/rbe/nogo_embed_deps/mixed",
)

nogo_test(
    name = "direct_nogo",
    config = "//:nogo_config",
    tags = ["manual"],
    deps = [":direct"],
)

nogo_test(
    name = "mixed_nogo",
    config = "//:nogo_config",
    tags = ["manual"],
    deps = [":mixed"],
)
