load("@com_google_protobuf//bazel/toolchains:proto_toolchain.bzl", "proto_toolchain")

package(default_visibility = ["//visibility:public"])

exports_files(["python.BUILD"])

filegroup(
    name = "version",
    srcs = ["@com_google_protobuf//bazel/private/oss/toolchains/prebuilt:authenticity_validation"],
    output_group = "_validation",
)

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

proto_toolchain(
    name = "incompatible_protoc",
    exec_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    proto_compiler = "@protoc_check_incompatible//:bin/protoc",
)
