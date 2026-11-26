load("//tools:defs.bzl", "go_test")

go_test(
    name = "coverage_lifecycle_test",
    srcs = ["coverage_lifecycle_test.go"],
    data = ["//debian:gvisor-release-coverage-tar-zstd"],
    local = True,
    tags = ["manual"],
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
)
