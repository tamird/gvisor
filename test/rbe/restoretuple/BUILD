load("@rules_python//python:defs.bzl", "py_test")

py_test(
    name = "tuple",
    srcs = ["tuple.py"],
    args = ["$(rootpath @july_runsc//file)"],
    data = ["@july_runsc//file"],
    tags = ["local"],
    timeout = "long",
)
