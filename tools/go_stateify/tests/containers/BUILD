load("//tools:defs.bzl", "go_library")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "containers",
    testonly = True,
    srcs = ["containers.go"],
    stateify = False,
    visibility = ["//tools/go_stateify/tests:__pkg__"],
)
