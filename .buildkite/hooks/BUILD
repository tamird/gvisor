load("@rules_python//python:py_test.bzl", "py_test")

py_test(
    name = "apt_setup_test",
    srcs = ["apt_setup_test.py"],
    data = ["pre-command"],
    # Exercise the native Ubuntu package-manager interface on this CI agent.
    tags = ["no-remote"],
)
