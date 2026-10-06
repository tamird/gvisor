load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//tools:defs.bzl", "bzl_library", "gotsan_flag_values", "gotsan_values", "py_test")

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
    name = "cc_compile_inputs_bzl",
    srcs = ["cc_compile_inputs.bzl"],
    visibility = ["//tools/clang_tidy:__pkg__"],
    deps = ["@rules_cc//cc/common"],
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
    deps = [
        "//tools/bazeldefs:cgroup_test_bzl",
        "//tools/bazeldefs:test_architectures_bzl",
    ],
)

bzl_library(
    name = "release_bzl",
    srcs = ["release.bzl"],
    visibility = [
        "//:sandbox",
    ],
)

exports_files([
    "gazelle-module-metadata.patch",
    "llvm-linux-header-default.patch",
    "with_cfg-test-frontend.patch",
])

exports_files(
    ["images.mk"],
    visibility = ["//test/docker:__pkg__"],
)

exports_files([
    "make_apt.sh",
    "make_release.sh",
    "make_test_key.sh",
])

py_test(
    name = "lint_test",
    srcs = ["lint_test.py"],
    args = ["$(rootpath :lint.sh)"],
    data = [
        "lint.sh",
        "//:.clang-format",
        "//:go.mod",
    ],
    main = "lint_test.py",
)

sh_test(
    name = "license_headers_test",
    size = "small",
    srcs = ["check_license_headers.sh"],
    args = [
        "--manifest",
        "$(rootpath @license_header_sources//:manifest)",
    ],
    data = [
        "@license_header_sources//:files",
        "@license_header_sources//:manifest",
    ],
    tags = ["manual"],
)
