load("@rules_python//python:py_test.bzl", "py_test")

# The benchmark uses the two runtimes registered on this Docker worker.
py_test(
    name = "paired_test",
    size = "large",
    timeout = "long",
    srcs = ["run.py"],
    args = ["$(rootpath benchstat)"],
    data = [
        "benchstat",
        "inputs.json",
        "startup_test",
    ],
    local = True,
    main = "run.py",
    tags = ["manual"],
)
