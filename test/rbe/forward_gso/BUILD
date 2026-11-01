load("@rules_cc//cc:cc_binary.bzl", "cc_binary")
load("@rules_python//python:defs.bzl", "py_test")

cc_binary(
    name = "router",
    srcs = ["router.c"],
    linkopts = ["-static"],
)

py_test(
    name = "forwarding_test",
    size = "large",
    srcs = ["forwarding_test.py"],
    data = [":router", "//:release"],
    local = True,
)
