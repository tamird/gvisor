load("@rules_foreign_cc_framework_toolchain_linux//:defs.bzl", "foreign_cc_framework_toolchain")
load("//tools:defs.bzl", "bzl_library", "cc_linker", "go_test")
load(":toolchain.bzl", "plugin_toolchain")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

exports_files(glob([
    "*.BUILD",
    "*.patch",
]))

bzl_library(
    name = "toolchain_bzl",
    srcs = ["toolchain.bzl"],
)

foreign_cc_framework_toolchain(
    name = "linux_commands",
    bash = "@plugin_programs//:bin/bash",
)

toolchain(
    name = "linux_amd64_bash",
    exec_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    toolchain = ":linux_commands",
    toolchain_type = "@rules_foreign_cc//foreign_cc/private/framework:shell_toolchain",
)

plugin_toolchain(
    name = "toolchain",
    exec_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    # The selected LLVM toolchain exposes these as declared tools rather than
    # legacy CcToolchainInfo paths.
    linker = cc_linker,
    objdump = "@llvm//tools:llvm-objdump",
    visibility = ["//visibility:public"],
)

go_test(
    name = "backtrace_test",
    size = "small",
    srcs = [
        "backtrace_test.go",
        "backtrace_unsafe.go",
    ],
    cdeps = ["@libpluginstack//:libpluginstack"],
    cgo = True,
    # The external DPDK plugin and its cgo sources only support Linux/AMD64.
    nogo = False,
    rundir = ".",
    static = True,
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
)
