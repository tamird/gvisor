load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//tools:defs.bzl", "bool_flag", "bzl_library", "gotsan_flag_values", "gotsan_values", "py_test")

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

# KCOV tests need an instrumented gVisor kernel even outside bazel coverage.
# Keep this setting on their runtime dependencies, leaving other tests alone.
bool_flag(
    name = "runtime_coverage",
    build_setting_default = False,
    visibility = ["//:sandbox"],
)

config_setting(
    name = "runtime_coverage_enabled",
    flag_values = {":runtime_coverage": "true"},
    visibility = ["//:sandbox"],
)

config_setting(
    name = "gotsan_runtime_coverage",
    flag_values = gotsan_flag_values | {":runtime_coverage": "true"},
    values = gotsan_values,
    visibility = ["//:sandbox"],
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
)

exports_files([
    "benchmark-umbrella-exports.patch",
    "gazelle-module-metadata.patch",
    "go-packages-provenance.patch",
    "llvm-linux-header-default.patch",
    "rules_go_benchmark_xml.patch",
    "rules_go_bootstrap.patch",
    "rules_go_package_provenance.patch",
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
    "release_repository.sh",
])

py_test(
    name = "lint_test",
    srcs = ["lint_test.py"],
    args = ["$(rootpath :lint.sh)"],
    data = ["lint.sh"],
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
