load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
load("//tools:defs.bzl", "cc_library")

package(default_visibility = ["//visibility:public"])

cc_library(
    name = "root",
    srcs = ["root.cc"],
    deps = ["//pkg/compile_commands_probe:dep"],
)

sh_binary(
    name = "preflight",
    srcs = ["preflight.sh"],
    args = ["$(rootpath //tools/compile_commands:compile_commands)"],
    data = ["//tools/compile_commands"],
)

sh_binary(
    name = "format",
    srcs = ["format.sh"],
)

sh_binary(
    name = "license_metadata",
    srcs = ["license_metadata.sh"],
)

sh_binary(
    name = "workspace",
    srcs = ["workspace.sh"],
)

sh_binary(
    name = "jammy",
    srcs = ["jammy.sh"],
    args = ["$(rootpath //tools/compile_commands:compile_commands)"],
    data = ["//tools/compile_commands"],
)

sh_binary(
    name = "order",
    srcs = ["order.sh"],
)
