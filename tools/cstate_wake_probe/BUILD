load("//tools:defs.bzl", "cc_binary")
load("//tools/lint:cc.bzl", "cc_lint_tests")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

cc_binary(
    name = "cstate_wake_probe",
    srcs = ["cstate_wake_probe.c"],
)

cc_lint_tests()
