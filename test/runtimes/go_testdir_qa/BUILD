load("@rules_python//python:defs.bzl", "py_test")

[py_test(
    name = mode,
    srcs = ["selection.py"],
    main = "selection.py",
    args = [mode],
    tags = ["local", "manual", "no-sandbox"],
    timeout = "eternal",
) for mode in ["controls", "full"]]
