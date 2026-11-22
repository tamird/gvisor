load("@bazel_skylib//rules:native_binary.bzl", "native_binary")

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
