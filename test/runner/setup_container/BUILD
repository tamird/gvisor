# setup_container contains a shim binary that runs within the test container
# for syscall tests with container=True.

load("//tools:defs.bzl", "cc_binary")
load("//tools/lint:cc.bzl", "cc_lint_tests")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

cc_binary(
    name = "setup_container",
    testonly = 1,
    srcs = ["setup_container.cc"],
    visibility = ["//test/runner:__subpackages__"],
    deps = [
        "//test/util:capability_util",
        "//test/util:file_descriptor",
        "//test/util:fs_util",
        "//test/util:posix_error",
        "//test/util:socket_util",
    ],
)

cc_lint_tests()
