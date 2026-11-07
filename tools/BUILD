load("@rules_python//python:py_test.bzl", "py_test")
load("//tools:defs.bzl", "bzl_library", "gazelle", "gotsan_flag_values", "gotsan_values")
load(":gazelle.bzl", "GAZELLE_PACKAGES")

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
    deps = [
        "@bazel_skylib//rules:native_binary",
        "@rules_shell//shell:rules_bzl",
    ],
)

bzl_library(
    name = "release_bzl",
    srcs = ["release.bzl"],
    visibility = [
        "//:sandbox",
    ],
    deps = [
        "//tools/bazeldefs:defs_bzl",
        "//tools/bazeldefs:go_bzl",
        "//tools/bazeldefs:pkg_bzl",
    ],
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

gazelle(name = "gazelle")

# Gazelle indexes go_library rules by importpath, which unmigrated BUILD files
# omit, so resolve gvisor.dev/gvisor imports by path instead (-index=none).
GAZELLE_ARGS = [
    "-index=none",
    "-r=false",
] + GAZELLE_PACKAGES

gazelle(
    name = "gazelle_check",
    args = GAZELLE_ARGS,
    mode = "diff",
)

gazelle(
    name = "gazelle_fix",
    args = GAZELLE_ARGS,
)

# Expose only the generated scripts to the repository-root entry points.
[
    alias(
        name = name + "_script",
        actual = ":" + name + "-runner",
        visibility = ["//:__pkg__"],
    )
    for name in ["gazelle", "gazelle_check", "gazelle_fix"]
]
