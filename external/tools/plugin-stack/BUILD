load("//tools:defs.bzl", "bzl_library", "go_test")
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
