package(default_visibility = ["//visibility:public"])

exports_files(["python.BUILD"])

toolchain(
    name = "static_python",
    exec_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    toolchain = "@protoc_check_static_python//:runtime",
    toolchain_type = "@bazel_tools//tools/python:toolchain_type",
)

toolchain(
    name = "static_python_arm64",
    exec_compatible_with = [
        "@platforms//cpu:aarch64",
        "@platforms//os:linux",
    ],
    target_compatible_with = [
        "@platforms//cpu:aarch64",
        "@platforms//os:linux",
    ],
    toolchain = "@protoc_check_static_python_arm64//:runtime",
    toolchain_type = "@bazel_tools//tools/python:toolchain_type",
)
