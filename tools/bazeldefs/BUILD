load("@llvm//platforms:common.bzl", "SUPPORTED_EXECS")
load("@llvm//toolchain:selects.bzl", "platform_resource_dir")
load("//tools:defs.bzl", "bool_flag", "bzl_library")
load(":platforms.bzl", "docker_exec_properties")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//:sandbox"],
    licenses = ["notice"],
)

# Shared by ordinary and namespace-dependent remote test actions.
_RBE_EXEC_PROPERTIES = {
    "OSFamily": "linux",
    "container-image": "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02",
    "workload-isolation-type": "oci",
}

platform(
    name = "rbe_linux_amd64",
    constraint_values = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    exec_properties = dict(
        _RBE_EXEC_PROPERTIES,
        Arch = "amd64",
    ),
)

# Only TestRunner actions need the privileged cgroup setup; compilation inherits
# the ordinary OCI worker properties.
platform(
    name = "rbe_linux_amd64_cgroup_v1",
    exec_properties = docker_exec_properties(free_disk = "20GB"),
    parents = [":rbe_linux_amd64"],
)

# C++ test execution can select the host platform. Retain the Go cgo constraints
# there while supplying the same remote routing as compilation actions.
platform(
    name = "rbe_linux_arm64",
    exec_properties = dict(
        _RBE_EXEC_PROPERTIES,
        Arch = "arm64",
    ),
    parents = ["@io_bazel_rules_go//go/toolchain:linux_arm64_cgo"],
)

config_setting(
    name = "rbe",
    define_values = {"rbe": "true"},
)

# Individual tests must be able to select the cgroup-v1 fixture. Bazel forbids
# Starlark transitions from changing --define, so use a build setting instead.
# https://github.com/bazelbuild/bazel/blob/d84820503/src/main/java/com/google/devtools/build/lib/analysis/starlark/FunctionTransitionUtil.java#L305-L313
bool_flag(
    name = "cgroup_v1",
    build_setting_default = False,
)

config_setting(
    name = "rbe_cgroup_v1",
    define_values = {"rbe": "true"},
    flag_values = {":cgroup_v1": "true"},
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
    deps = [
        ":cgroup_test_bzl",
        ":test_architectures_bzl",
    ],
)

bzl_library(
    name = "go_bzl",
    srcs = ["go.bzl"],
    visibility = ["//visibility:private"],
    deps = [
        ":cgroup_test_bzl",
        ":defs_bzl",
        ":go_variants_bzl",
        ":test_architectures_bzl",
    ],
)

bzl_library(
    name = "go_variants_bzl",
    srcs = ["go_variants.bzl"],
    visibility = ["//visibility:private"],
    deps = [
        ":cgroup_test_bzl",
        ":defs_bzl",
        ":test_architectures_bzl",
    ],
)

bzl_library(
    name = "cgroup_test_bzl",
    srcs = ["cgroup_test.bzl"],
    visibility = ["//:sandbox"],
    deps = ["@with_cfg.bzl//:with_cfg"],
)

bzl_library(
    name = "test_architectures_bzl",
    srcs = ["test_architectures.bzl"],
    visibility = ["//:sandbox"],
    deps = ["@with_cfg.bzl//:with_cfg"],
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
