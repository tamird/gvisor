load("//tools:defs.bzl", "go_library")

package(default_applicable_licenses = ["//:license"])

go_library(
    name = "testutil",
    testonly = True,
    srcs = ["wedged.go"],
    importpath = "gvisor.dev/gvisor/pkg/shim/v1/testutil",
    visibility = ["//pkg/shim/v1:__subpackages__"],
    deps = ["@org_golang_x_sys//unix:go_default_library"],
)
