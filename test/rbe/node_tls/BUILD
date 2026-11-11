load("@rules_python//python:defs.bzl", "py_test")

py_test(
    name = "loopback_test",
    srcs = ["loopback_test.py"],
    data = ["reviewed.js"],
    legacy_create_init = False,
    tags = ["local"],
    timeout = "moderate",
)
