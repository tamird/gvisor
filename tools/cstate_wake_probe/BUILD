load("//tools:defs.bzl", "cc_binary")
load("//tools/lint:defs.bzl", "clang_format_inputs")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

cc_binary(
    name = "cstate_wake_probe",
    srcs = ["cstate_wake_probe.c"],
)

clang_format_inputs(name = "clang_format_srcs")
