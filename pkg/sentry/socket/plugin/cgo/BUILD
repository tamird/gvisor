load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

config_setting(
    name = "network_plugins",
    values = {"define": "network_plugins=true"},
)

_PLUGIN_CDEPS = select({
    ":network_plugins": ["@libpluginstack//:libpluginstack"],
    "//conditions:default": [],
})

go_library(
    name = "cgo",
    srcs = [
        "cgo.go",
        "nocgo_stub_unsafe.go",
        "socket_unsafe.go",
        "stack_unsafe.go",
        "util_unsafe.go",
    ],
    bazel_cdeps = _PLUGIN_CDEPS,
    bazel_cgo = select({
        ":network_plugins": True,
        "//conditions:default": False,
    }),
    visibility = ["//visibility:public"],
    deps = [
        "//pkg/abi/linux",
        "//pkg/abi/linux/errno",
    ],
)

go_test(
    name = "backtrace_test",
    size = "small",
    srcs = [
        "testdata/backtrace_test.go",
        "testdata/backtrace_unsafe.go",
    ],
    cdeps = _PLUGIN_CDEPS,
    cgo = True,
    # Nogo also analyzes ARM64 and pure Go. The TLDK archive requires AMD64 and cgo.
    nogo = False,
    static = True,
    tags = ["manual"],  # Run with --config=plugin-tldk.
)
