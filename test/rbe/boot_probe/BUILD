load("@io_bazel_rules_go//go:def.bzl", "go_binary")

go_binary(
    name = "boot_probe",
    testonly = True,
    srcs = ["main.go"],
    pure = "on",
    race = "off",
    static = "on",
    visibility = ["//visibility:public"],
)
