load("//tools:defs.bzl", "go_binary", "go_reset_target", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_binary(
    name = "go_merge",
    srcs = ["main.go"],
    visibility = ["//:sandbox"],
    deps = [
        "//tools/constraintutil",
    ],
)

go_reset_target(
    name = "go_merge_tool",
    dep = ":go_merge",
    visibility = ["//:sandbox"],
)

go_test(
    name = "go_merge_test",
    size = "small",
    srcs = [
        "main.go",
        "main_test.go",
    ],
    deps = ["//tools/constraintutil"],
)

# @unused
glaze_ignore = [
    "main.go",
    "main_test.go",
]
