load("//tools:defs.bzl", "go_library")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//:sandbox"],
    licenses = ["notice"],
)

go_library(
    name = "genericfstree",
    srcs = [
        "genericfstree.go",
    ],
    visibility = ["//visibility:public"],
    deps = [
        "//pkg/fspath",
        "//pkg/sentry/vfs",
    ],
)
