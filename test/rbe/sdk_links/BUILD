load("@io_bazel_rules_go//go/private:common.bzl", "RULES_GO_STDLIB_PREFIX")
load("@io_bazel_rules_go//go/private/rules:binary.bzl", "go_tool_binary")
load(":defs.bzl", "relative_sdk")

exports_files(["diagnostic-path.patch"])

config_setting(
    name = "only_readlink",
    define_values = {"SDK_LINK_PATH": "readlink"},
)

config_setting(
    name = "both_helpers",
    define_values = {"SDK_LINK_PATH": "both"},
)

relative_sdk(
    name = "relative",
    helpers = select({
        ":only_readlink": ["readlink"],
        ":both_helpers": ["dirname", "readlink"],
        "//conditions:default": [],
    }),
    sdk = "@@rules_go++go_sdk+main___download_0_linux_amd64//:go_sdk",
)

filegroup(
    name = "witness",
    srcs = [":relative"],
    output_group = "witness",
)

go_tool_binary(
    name = "builder",
    srcs = ["@io_bazel_rules_go//go/tools/builders:builder_srcs"],
    ldflags = "-X main.rulesGoStdlibPrefix={}".format(RULES_GO_STDLIB_PREFIX),
    out_covdata = "covdata.exe",
    out_pack = "pack.exe",
    sdk = ":relative",
)
