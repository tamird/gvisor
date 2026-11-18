load("@rules_python//python:py_binary.bzl", "py_binary")
load("@rules_python//python:py_library.bzl", "py_library")
load("@rules_python//python:py_test.bzl", "py_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

py_binary(
    name = "credential_helper",
    srcs = ["credential_helper.py"],
    visibility = ["//visibility:public"],
)

py_library(
    name = "credential_helper_lib",
    srcs = ["credential_helper.py"],
    imports = ["."],
)

py_test(
    name = "credential_helper_test",
    srcs = ["credential_helper_test.py"],
    deps = [":credential_helper_lib"],
)
