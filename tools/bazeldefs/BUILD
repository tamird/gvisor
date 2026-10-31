load("@bazel_lib//lib:run_binary.bzl", "run_binary")
load("//tools:defs.bzl", "bool_flag", "bzl_library", "string_flag")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//:sandbox"],
    licenses = ["notice"],
)

bzl_library(
    name = "platforms_bzl",
    srcs = ["platforms.bzl"],
    visibility = ["//:sandbox"],
)

bzl_library(
    name = "tags_bzl",
    srcs = ["tags.bzl"],
    visibility = ["//visibility:private"],
)

bzl_library(
    name = "defs_bzl",
    srcs = ["defs.bzl"],
    visibility = ["//visibility:private"],
    deps = [
        "@bazel_skylib//rules/directory:providers",
        "@rules_cc//cc/common",
    ],
)

bzl_library(
    name = "go_bzl",
    srcs = ["go.bzl"],
    visibility = ["//visibility:private"],
    deps = [":defs_bzl"],
)

# Keep Go's pure/cgo mode in its build settings, not the target platform:
# Bazel 9 also uses target constraints to select a test execution platform.
platform(
    name = "linux_amd64",
    constraint_values = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    visibility = ["//visibility:public"],
)

platform(
    name = "linux_arm64",
    constraint_values = [
        "@platforms//cpu:aarch64",
        "@platforms//os:linux",
    ],
    visibility = ["//visibility:public"],
)

config_setting(
    name = "amd64",
    constraint_values = [
        "@platforms//cpu:x86_64",
    ],
)

config_setting(
    name = "arm64",
    constraint_values = [
        "@platforms//cpu:aarch64",
    ],
)

config_setting(
    name = "riscv64",
    constraint_values = [
        "@platforms//cpu:riscv64",
    ],
)

bool_flag(
    name = "release_stamp",
    build_setting_default = False,
)

config_setting(
    name = "staged_release",
    flag_values = {":release_stamp": "true"},
)

run_binary(
    name = "version",
    outs = ["version.txt"],
    mnemonic = "PackageVersion",
    args = select({
        ":staged_release": ["BUILD_EMBED_LABEL"],
        "//conditions:default": ["STABLE_VERSION"],
    }) + ["$@"],
    stamp = 1,
    tags = [
        "manual",
        "nobuilder",
        "notap",
    ],
    tool = "//tools/package_version",
    visibility = ["//:sandbox"],
)

# A build setting lets individual test targets select their page size without
# changing unrelated --define values in the same invocation.
string_flag(
    name = "page_size",
    build_setting_default = "4k",
    values = [
        "4k",
        "64k",
    ],
)

# Page size configurations for ARM64
config_setting(
    name = "pagesize_64k",
    flag_values = {":page_size": "64k"},
)

# Combined config: ARM64 with 64K page size
config_setting(
    name = "arm64_pagesize_64k",
    constraint_values = [
        "@platforms//cpu:aarch64",
    ],
    flag_values = {":page_size": "64k"},
)
