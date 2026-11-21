load("//tools:defs.bzl", "cc_binary")
load("//tools/lint:defs.bzl", "clang_format_inputs")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

cc_binary(
    name = "compressor",
    srcs = ["compressor.c"],
    visibility = ["//debian:__pkg__"],
    deps = ["@llvm_zstd//:zstd"],
)

clang_format_inputs(name = "clang_format_srcs")
