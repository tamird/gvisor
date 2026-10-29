load("//tools:defs.bzl", "cc_binary")
load("//tools/lint:cc.bzl", "cc_lint_tests")

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

cc_lint_tests()
