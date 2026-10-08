load("//tools:defs.bzl", "bzl_library", "go_binary")
load(":diagnostic.bzl", "profile_decoder")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_binary(
    name = "profiletool",
    srcs = ["profiletool.go"],
    visibility = [
        "//:sandbox",
    ],
    deps = [
        "//pkg/log",
        "//runsc/flag",
        "@com_github_google_pprof//profile:go_default_library",
    ],
)

# Fork-only decoder for the existing state benchmark profiles.
profile_decoder(
    name = "state_pprof_decoder",
    tags = ["manual"],
)

bzl_library(
    name = "diagnostic_bzl",
    srcs = ["diagnostic.bzl"],
    deps = ["//tools/bazeldefs:go_bzl"],
)
