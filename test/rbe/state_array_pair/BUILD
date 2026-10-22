load("@rules_python//python:py_test.bzl", "py_test")

# The benchmark uses the two runtimes registered on this Docker worker.
py_test(
    name = "paired_test",
    size = "large",
    timeout = "long",
    srcs = ["run.py"],
    args = ["$(rootpath @org_golang_x_perf//cmd/benchstat)"],
    data = [
        "inputs.json",
        "startup_test",
        "@org_golang_x_perf//cmd/benchstat",
    ],
    local = True,
    main = "run.py",
    tags = ["manual"],
)
