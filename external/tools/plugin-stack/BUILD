load("//tools:defs.bzl", "bzl_library")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

exports_files(glob([
    "*.BUILD",
    "*.patch",
    "testdata/*.go",
]))

bzl_library(
    name = "toolchain_bzl",
    srcs = ["toolchain.bzl"],
    deps = ["@rules_cc//cc:find_cc_toolchain_bzl"],
)
