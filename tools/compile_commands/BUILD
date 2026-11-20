load("@bazel_skylib//rules:native_binary.bzl", "native_binary")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//visibility:public"],
    licenses = ["notice"],
)

config_setting(
    name = "linux_amd64",
    constraint_values = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
)

config_setting(
    name = "linux_arm64",
    constraint_values = [
        "@platforms//cpu:aarch64",
        "@platforms//os:linux",
    ],
)

native_binary(
    name = "compile_commands",
    src = select({
        ":linux_amd64": "@bazel_compile_commands_linux_amd64//:usr/bin/bazel-compile-commands",
        ":linux_arm64": "@bazel_compile_commands_linux_arm64//:usr/bin/bazel-compile-commands",
        "@platforms//os:macos": "@bazel_compile_commands_macos_universal//:usr/bin/bazel-compile-commands",
    }),
)
