load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "tests",
    testonly = True,
    srcs = ["types.go"],
    imports = ["gvisor.dev/gvisor/tools/go_stateify/tests/containers"],
    deps = ["//tools/go_stateify/tests/containers"],
)

go_test(
    name = "tests_test",
    size = "small",
    srcs = ["types_test.go"],
    embed = [":tests"],
    deps = [
        "//pkg/state",
        "//tools/go_stateify/tests/containers",
    ],
)
