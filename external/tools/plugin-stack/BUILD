load("//tools:defs.bzl", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

exports_files(glob([
    "*.BUILD",
    "*.patch",
]))

config_setting(
    name = "plugin_tldk",
    values = {"define": "plugin_tldk=true"},
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
