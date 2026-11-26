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

# Execution properties select native workers without changing target toolchains.
_REMOTE_IMAGE = "docker://gcr.io/flame-public/rbe-ubuntu24-04@sha256:be9853fc45092d2cf2bda3abed30d22182881659a50bf35c7125161d7cfc8a75"

_REMOTE_EXEC_PROPERTIES = {
    "OSFamily": "linux",
    "container-image": _REMOTE_IMAGE,
    # The remote server controls networking, outside Bazel's local sandbox.
    # Firecracker otherwise permits external access; keep localhost only:
    # https://www.buildbuddy.io/docs/rbe-platforms/#runner-container-support
    "network": "off",
}

# Native C tools are supplied by the pinned RBE image, not the submitting host.
constraint_setting(name = "c_execution_environment")

constraint_value(
    name = "rbe_ubuntu24_c",
    constraint_setting = ":c_execution_environment",
)

bool_flag(
    name = "rbe_native_c",
    build_setting_default = False,
)

config_setting(
    name = "rbe_native_c_enabled",
    flag_values = {":rbe_native_c": "true"},
)

config_setting(
    name = "rbe_native_c_disabled",
    flag_values = {":rbe_native_c": "false"},
)

platform(
    name = "remote_linux_amd64",
    constraint_values = [":rbe_ubuntu24_c"],
    exec_properties = dict(
        _REMOTE_EXEC_PROPERTIES,
        Arch = "amd64",
    ),
    parents = [":linux_amd64"],
    visibility = ["//visibility:public"],
)

platform(
    name = "remote_linux_arm64",
    constraint_values = [":rbe_ubuntu24_c"],
    exec_properties = dict(
        _REMOTE_EXEC_PROPERTIES,
        Arch = "arm64",
    ),
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
