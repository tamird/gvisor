load("//tools:defs.bzl", "go_library")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "cgo",
    srcs = [
        "cgo.go",
        "nocgo_stub_unsafe.go",
        "socket_unsafe.go",
        "stack_unsafe.go",
        "util_unsafe.go",
    ],
    bazel_cdeps = select({
        "//external/tools/plugin-stack:plugin_tldk": ["@libpluginstack//:libpluginstack"],
        "//conditions:default": [],
    }),
    bazel_cgo = select({
        "//external/tools/plugin-stack:plugin_tldk": True,
        "//conditions:default": False,
    }),
    visibility = ["//visibility:public"],
    deps = [
        "//pkg/abi/linux",
        "//pkg/abi/linux/errno",
    ],
)
