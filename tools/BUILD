load("//tools:defs.bzl", "bzl_library", "gotsan_flag_values", "gotsan_values")
load("//tools/bazeldefs:py_test.bzl", "py_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

config_setting(
    name = "debug",
    values = {"compilation_mode": "dbg"},
    visibility = [
        "//:sandbox",
    ],
)

config_setting(
    name = "gotsan",
    flag_values = gotsan_flag_values,
    values = gotsan_values,
    visibility = [
        "//:sandbox",
    ],
)

config_setting(
    name = "lockdep",
    values = {"define": "lockdep=true"},
    visibility = [
        "//:sandbox",
    ],
)

bzl_library(
    name = "arch_bzl",
    srcs = ["arch.bzl"],
    visibility = [
        "//:sandbox",
    ],
)

bzl_library(
    name = "defs_bzl",
    srcs = ["defs.bzl"],
    visibility = [
        "//:sandbox",
    ],
)

bzl_library(
    name = "deps_bzl",
    srcs = ["deps.bzl"],
    visibility = [
        "//:sandbox",
    ],
    deps = ["//tools/bazeldefs:test_architectures_bzl"],
)

bzl_library(
    name = "gazelle_bzl",
    srcs = ["gazelle.bzl"],
    visibility = [
        "//:sandbox",
    ],
)

bzl_library(
    name = "release_bzl",
    srcs = ["release.bzl"],
    visibility = [
        "//:sandbox",
    ],
    deps = [":defs_bzl"],
)

exports_files([
    "benchmark-umbrella-exports.patch",
    "gazelle-native-environment.patch",
    "llvm-freestanding-runtimes.patch",
    "llvm-linux-header-default.patch",
    "grpc_plugin_symlink.patch",
    "protobuf_authenticity.patch",
    "protobuf_stage1.patch",
    "rules_go_bootstrap_shell.patch",
])

py_test(
    name = "lint_test",
    srcs = ["lint_test.py"],
    args = ["$(rootpath :lint.sh)"],
    data = [
        "lint.sh",
        "//:.clang-format",
    ],
    main = "lint_test.py",
)
