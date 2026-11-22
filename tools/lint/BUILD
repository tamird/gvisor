load("@bazel_skylib//rules:native_binary.bzl", "native_binary")

package(default_visibility = ["//visibility:public"])

# LLVM dispatches its multicall binary by the basename in argv[0].
native_binary(
    name = "clang_format_probe_tool",
    src = "@llvm//tools:clang-format",
    out = "bin/clang-format",
)
