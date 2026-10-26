load("//tools:defs.bzl", "go_binary", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_binary(
    name = "tpucheckpoint",
    srcs = ["tpucheckpoint.go"],
    visibility = ["//:sandbox"],
    deps = [
        "//tools/tpucheckpoint/tpucontrol",
    ],
)

go_test(
    name = "tpucheckpoint_test",
    size = "small",
    srcs = ["tpucheckpoint_test.go"],
    data = [":tpucheckpoint"],
    deps = ["//pkg/test/testutil"],
)
