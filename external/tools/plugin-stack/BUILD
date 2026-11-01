load("//tools:defs.bzl", "bzl_library", "cc_linker")
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
