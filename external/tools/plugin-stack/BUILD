load("//tools:defs.bzl", "bool_flag", "bzl_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

exports_files(glob([
    "*.BUILD",
    "*.patch",
]))

bool_flag(
    name = "tldk",
    build_setting_default = False,
    visibility = ["//visibility:public"],
)

config_setting(
    name = "plugin_tldk",
    flag_values = {":tldk": "true"},
    visibility = ["//visibility:public"],
)

bzl_library(
    name = "defs_bzl",
    srcs = ["defs.bzl"],
    visibility = ["//:sandbox"],
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
    # Nogo always analyzes ARM64 too; this native plugin only supports AMD64.
    nogo = False,
    rundir = ".",
    static = True,
    target_compatible_with = select({
        ":plugin_tldk": [
            "@platforms//cpu:x86_64",
            "@platforms//os:linux",
        ],
        "//conditions:default": ["@platforms//:incompatible"],
    }),
)
