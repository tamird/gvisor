load("//tools:defs.bzl", "bzl_library")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//:sandbox"],
    licenses = ["notice"],
)

bzl_library(
    name = "coverage_analysis_test_bzl",
    srcs = ["coverage_analysis_test.bzl"],
    deps = ["@io_bazel_rules_go//go:def"],
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
    deps = [
        ":defs_bzl",
        "@with_cfg.bzl//:with_cfg",
    ],
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

# Match the userspace ABI of the Ubuntu 24.04 C/C++ build workers.
_REMOTE_IMAGE = "docker://gcr.io/flame-public/rbe-ubuntu24-04@sha256:be9853fc45092d2cf2bda3abed30d22182881659a50bf35c7125161d7cfc8a75"

# Execution properties select native workers without changing target toolchains.
platform(
    name = "remote_linux_amd64",
    exec_properties = {
        "Arch": "amd64",
        "OSFamily": "linux",
        "container-image": _REMOTE_IMAGE,
    },
    parents = [":linux_amd64"],
    visibility = ["//visibility:public"],
)

platform(
    name = "remote_linux_arm64",
    exec_properties = {
        "Arch": "arm64",
        "OSFamily": "linux",
        "container-image": _REMOTE_IMAGE,
    },
    parents = [":linux_arm64"],
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
