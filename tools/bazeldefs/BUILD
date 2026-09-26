load("@llvm//platforms:common.bzl", "SUPPORTED_EXECS")
load("@llvm//toolchain:selects.bzl", "platform_resource_dir")
load("//tools:defs.bzl", "bzl_library")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//:sandbox"],
    licenses = ["notice"],
)

# BPF uses the downloaded compiler and its matching resource directory.
alias(
    name = "bpf_clang",
    actual = select({
        "@llvm//platforms/config:{}_{}".format(os, cpu): platform_resource_dir(os, cpu).same_package_label("bin/clang.exe" if os == "windows" else "bin/clang")
        for os, cpu in SUPPORTED_EXECS
    }),
)

alias(
    name = "clang_resource_dir",
    actual = select({
        "@llvm//platforms/config:{}_{}".format(os, cpu): platform_resource_dir(os, cpu)
        for os, cpu in SUPPORTED_EXECS
    }),
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
)

bzl_library(
    name = "go_bzl",
    srcs = ["go.bzl"],
    visibility = ["//visibility:private"],
    deps = [
        ":defs_bzl",
        ":go_static_bzl",
    ],
)

bzl_library(
    name = "go_static_bzl",
    srcs = ["go_static.bzl"],
    visibility = ["//visibility:private"],
    deps = [":defs_bzl"],
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

# Page size configurations for ARM64
config_setting(
    name = "pagesize_64k",
    define_values = {"pagesize": "64k"},
)

# Combined config: ARM64 with 64K page size
config_setting(
    name = "arm64_pagesize_64k",
    constraint_values = [
        "@platforms//cpu:aarch64",
    ],
    define_values = {"pagesize": "64k"},
)
