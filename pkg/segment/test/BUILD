load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//visibility:private"],
    licenses = ["notice"],
)

go_library(
    name = "segment",
    testonly = 1,
    srcs = [
        "set_functions.go",
    ],
    deps = [
        "//pkg/segment",
        "//pkg/segment/set",
        "//pkg/segment/setgaps",
        "//pkg/state",
    ],
)

go_test(
    name = "segment_test",
    size = "small",
    srcs = ["segment_test.go"],
    library = ":segment",
)
