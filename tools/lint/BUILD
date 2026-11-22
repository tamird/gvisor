load("@bazel_skylib//rules:native_binary.bzl", "native_binary")
load("@aspect_rules_lint//format:defs.bzl", "format_multirun", "format_test")
load(":native_preflight.bzl", "check", "inventory")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

# LLVM dispatches its multicall binary by the basename in argv[0].
native_binary(
    name = "clang_format_tool",
    src = "@llvm//tools:clang-format",
    out = "bin/clang-format",
)

format_multirun(
    name = "native_preflight_format",
    c = ":clang_format_tool",
    cc = ":clang_format_tool",
    jobs = 1,
    disable_git_attribute_checks = True,
)

inventory(
    name = "native_preflight_inputs",
    srcs = ["//test/util:eventfd_util.h", "//:clang_format_config"],
)

format_test(
    name = "native_preflight_test",
    srcs = [":native_preflight_inputs"],
    cc = ":clang_format_tool",
    disable_git_attribute_checks = True,
)

check(
    name = "native_preflight_check",
    srcs = [":native_preflight_inputs"],
    checker = ":native_preflight_format.check",
)
