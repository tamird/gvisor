load("@bazel_skylib//rules:native_binary.bzl", "native_binary")
load("@go_host_compatible_sdk_label//:defs.bzl", "HOST_COMPATIBLE_SDK")
load(":defs.bzl", "shell_toolchain")

filegroup(
    name = "helpers",
    srcs = [
        HOST_COMPATIBLE_SDK.same_package_label("builder"),
        HOST_COMPATIBLE_SDK.same_package_label("pack.exe"),
    ],
    visibility = ["//visibility:public"],
)

# The executable name selects BusyBox's ash applet; stage it without a shell.
native_binary(
    name = "static_ash",
    src = select({
        "@platforms//cpu:x86_64": "@bootstrap_busybox//:payload/usr/bin/busybox",
        "@platforms//cpu:aarch64": "@bootstrap_busybox_arm64//:payload/usr/bin/busybox",
    }),
    out = "ash",
)

shell_toolchain(
    name = "declared_ash",
    interpreter = ":static_ash",
)

toolchain(
    name = "linux_amd64_ash",
    exec_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    toolchain = ":declared_ash",
    toolchain_type = "@rules_shell//shell:exec_toolchain_type",
)
toolchain(
    name = "linux_arm64_ash",
    exec_compatible_with = [
        "@platforms//cpu:aarch64",
        "@platforms//os:linux",
    ],
    toolchain = ":declared_ash",
    toolchain_type = "@rules_shell//shell:exec_toolchain_type",
)
