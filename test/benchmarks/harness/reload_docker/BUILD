load("//tools:defs.bzl", "go_binary")

package(default_applicable_licenses = ["//:license"])

go_binary(
    name = "reload_docker",
    srcs = ["main.go"],
    out = "reload_docker",
    target_compatible_with = ["@platforms//os:linux"],
    visibility = ["//test/benchmarks/harness:__pkg__"],
    deps = ["@org_golang_x_sys//unix:go_default_library"],
)
