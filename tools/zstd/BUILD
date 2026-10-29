load("//tools:defs.bzl", "cc_binary")
load("//tools/lint:cpplint.bzl", "cpplint_test")

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

cpplint_test()
