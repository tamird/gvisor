load("@rules_python//python:py_test.bzl", "py_test")

py_test(
    name = "queue",
    size = "enormous",
    srcs = ["matrix.py"],
    args = [
        "$(rootpath :stock)",
        "$(rootpath :fixed)",
        "$(rootpath :calibration)",
    ],
    data = [
        "calibration",
        "fixed",
        "stock",
    ],
    main = "matrix.py",
)
