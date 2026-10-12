load("//tools:defs.bzl", "go_binary", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_binary(
    name = "retag_archive",
    srcs = ["main.go"],
    features = ["fully_static_link"],
    pure = "on",
    visibility = ["//test/docker:__pkg__"],
)

go_test(
    name = "retag_archive_test",
    size = "small",
    srcs = [
        "main.go",
        "main_test.go",
    ],
)
