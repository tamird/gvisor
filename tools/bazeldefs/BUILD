load("//tools:defs.bzl", "bzl_library", "string_flag")
load(":cc.bzl", "using_llvm")

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

using_llvm(name = "using_llvm")

# Use LLVM's musl sysroot for static race links on the project platforms and
# on an unmodified Linux host. Do not replace a custom target or toolchain.
[
    config_setting(
        name = "race_link_" + name,
        constraint_values = [
            "@platforms//cpu:" + cpu,
            "@platforms//os:linux",
        ],
        flag_values = {":using_llvm": "True"},
        values = options,
    )
    for name, cpu, options in [
        (
            "amd64",
            "x86_64",
            {"platforms": "//tools/bazeldefs:linux_amd64"},
        ),
        (
            "arm64",
            "aarch64",
            {"platforms": "//tools/bazeldefs:linux_arm64"},
        ),
        (
            "native_amd64",
            "x86_64",
            {
                "host_platform": "@bazel_tools//tools:host_platform",
                "platforms": "@bazel_tools//tools:host_platform",
            },
        ),
        (
            "native_arm64",
            "aarch64",
            {
                "host_platform": "@bazel_tools//tools:host_platform",
                "platforms": "@bazel_tools//tools:host_platform",
            },
        ),
    ]
]

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

genrule(
    name = "version",
    outs = ["version.txt"],
    cmd = "cat bazel-out/stable-status.txt | grep STABLE_VERSION | cut -d' ' -f2- | sed 's/^[^[:digit:]]*//g' >$@",
    stamp = 1,
    tags = [
        "manual",
        "nobuilder",
        "notap",
    ],
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
