load("//tools:defs.bzl", "go_library", "select_arch", "sh_test")
load("//tools/nogo:defs.bzl", "nogo_config", "nogo_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "sanity",
    srcs = ["sanity.go"],
    # This would fail, so we need to disable here. Instead, we manually
    # construct the nogo test below, and ensure that it is failing.
    nogo = False,
)

nogo_config(
    name = "sanity_config",
    srcs = ["sanity.yaml"],
)

nogo_test(
    name = "sanity_nogo",
    srcs = ["sanity.go"],
    config = ":sanity_config",
    # sanity_test runs this script as data; its embedded analyzer must run on
    # the same architecture as the enclosing test.
    exec_compatible_with = ["@platforms//os:linux"] + select_arch(
        amd64 = ["@platforms//cpu:x86_64"],
        arm64 = ["@platforms//cpu:aarch64"],
    ),
    tags = [
        "manual",
        "noguitar",
        "notap",
    ],
    deps = [":sanity"],
)

sh_test(
    name = "sanity_test",
    srcs = ["sanity_test.sh"],
    args = ["$(location :sanity_nogo)"],
    data = [":sanity_nogo"],
)
