load("//tools:defs.bzl", "go_binary")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_binary(
    name = "stage",
    srcs = ["stage.go"],
    out = "stage",
    pure = True,
    visibility = ["//debian:__pkg__"],
)
