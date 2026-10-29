load("//tools:defs.bzl", "cc_binary")
load("//tools/lint:cpplint.bzl", "cpplint_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

cc_binary(
    name = "cstate_wake_probe",
    srcs = ["cstate_wake_probe.c"],
)

cpplint_test()
