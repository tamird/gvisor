load("@rules_python//python:py_binary.bzl", "py_binary")
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

py_test(
    name = "credential_helper_test",
    srcs = [
        "credential_helper.py",
        "credential_helper_test.py",
    ],
)
