load("//tools:defs.bzl", "go_library", "go_test")

package(default_applicable_licenses = ["//:license"])

go_library(
    name = "codegen",
    srcs = [
        "calls.go",
        "inline.go",
    ],
    stateify = False,
    visibility = ["//pkg/sentry/platform/kvm:__pkg__"],
    deps = ["//tools/checkescape"],
)

go_test(
    name = "codegen_test",
    size = "small",
    srcs = [
        "calls_test.go",
        "inline_test.go",
    ],
    data = ["//pkg/sentry/platform/kvm:atomicptr_codegen_source_data"],
    embed = [":codegen"],
    deps = [
        "//pkg/test/testutil",
        "//tools/checkescape",
    ],
)
