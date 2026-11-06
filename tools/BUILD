load("@rules_python//python:py_test.bzl", "py_test")
load("//tools:defs.bzl", "bzl_library", "gotsan_flag_values", "gotsan_values")

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
    "build_cover.sh",
    "go-allow-internal.patch",
    "llvm-linux-header-default.patch",
])

py_test(
    name = "lint_test",
    srcs = ["lint_test.py"],
    args = ["$(rootpath :lint.sh)"],
    data = [
        "lint.sh",
        "//:.clang-format",
    ],
)

py_test(
    name = "apt_native_signature_test",
    srcs = ["apt_native_signature_test.py"],
    args = [
        "$(rootpath :make_apt.sh)",
        "$(rootpath @relic_apt_probe//file)",
        "$(rootpath @dpkg_sig_oracle//file)",
        "$(rootpath @dpkg_sig_oracle_config//file)",
        "$(rootpath @released_runsc_amd64//file)",
        "$(rootpath @released_runsc_arm64//file)",
    ],
    data = [
        ":make_apt.sh",
        "@dpkg_sig_oracle//file",
        "@dpkg_sig_oracle_config//file",
        "@released_runsc_amd64//file",
        "@released_runsc_arm64//file",
        "@relic_apt_probe//file",
    ],
    tags = ["local"],
    timeout = "long",
)
