load("//tools:defs.bzl", "bzl_library")

package(default_applicable_licenses = ["//:license"])

bzl_library(
    name = "defs_bzl",
    srcs = ["defs.bzl"],
    visibility = ["//visibility:private"],
    deps = [
        "//tools/bazeldefs:cgroup_test_bzl",
        "//tools/bazeldefs:test_architectures_bzl",
    ],
)
