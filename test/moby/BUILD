load("//tools:defs.bzl", "bzl_library", "go_binary", "more_shards")
load(":defs.bzl", "moby_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_binary(
    name = "runner",
    srcs = ["moby_runner.go"],
    static = True,
    deps = [
        "//test/runtimes/proctor",
        "//test/runtimes/proctor/lib",
    ],
)

# The proctor is copied as file data. Its static executable alias and default
# output have different paths; expose one File for argument expansion.
filegroup(
    name = "runner_file",
    srcs = [":runner"],
)

# Both entrypoints share the public sources, inputs and four shards.
moby_test(
    name = "moby",
    srcs = ["moby_test.go"],
    args = ["--proctor=$(rootpath :runner_file)"],
    data = [
        ":runner_file",
        "//runsc",
    ],
    shard_count = more_shards,
    deps = [
        "//pkg/test/dockerutil",
        "//pkg/test/testutil",
        "//test/runtimes/runner/lib",
    ],
)

bzl_library(
    name = "defs_bzl",
    srcs = ["defs.bzl"],
    deps = [
        "//test/docker:defs_bzl",
        "//tools:defs_bzl",
        "@io_bazel_rules_go//go:def",
        "@with_cfg.bzl//:with_cfg",
    ],
)
