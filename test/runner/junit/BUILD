load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "junit",
    srcs = ["junit.go"],
    importpath = "gvisor.dev/gvisor/test/runner/junit",
    stateify = False,
    visibility = ["//test/runner:__pkg__"],
)

go_test(
    name = "junit_test",
    size = "small",
    srcs = ["junit_test.go"],
    embed = [":junit"],
)
