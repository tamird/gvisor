package(default_visibility = ["//visibility:public"])

exports_files(["python.BUILD"])

toolchain(
    name = "amd64_musl",
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
        "@llvm//constraints/libc:musl",
    ],
    toolchain = "@release_static_cpython//:runtime",
    toolchain_type = "@bazel_tools//tools/python:toolchain_type",
)

toolchain(
    name = "arm64_musl",
    target_compatible_with = [
        "@platforms//cpu:aarch64",
        "@platforms//os:linux",
        "@llvm//constraints/libc:musl",
    ],
    toolchain = "@release_static_cpython_arm64//:runtime",
    toolchain_type = "@bazel_tools//tools/python:toolchain_type",
)
