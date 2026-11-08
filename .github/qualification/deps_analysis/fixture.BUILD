load("@io_bazel_rules_go//go:def.bzl", "go_binary")
load("//tools:deps.bzl", "deps_test")
load(":fixture.bzl", "vertex")

vertex(name = "ordinary")
vertex(name = "price$usd")
vertex(name = "arm_only")
vertex(name = "allowed_separately")
vertex(name = "invalid_validation", invalid = True)

deps_test(
    name = "validation_test",
    targets = [":invalid_validation"],
)

vertex(
    name = "source",
    deps = [
        ":ordinary",
        ":price$usd",
    ] + select({
        "@platforms//cpu:aarch64": [":arm_only"],
        "//conditions:default": [],
    }),
)

deps_test(
    name = "forbidden_test",
    allowed = [":allowed_separately"],
    targets = [":source"],
)

go_binary(
    name = "reader",
    srcs = ["reader.go"],
    pure = "on",
    static = "on",
    visibility = ["//visibility:public"],
)
