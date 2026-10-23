load("@rules_python//python:py_test.bzl", "py_test")

# One current benchmark and runtime compare two immutable server fixtures.
py_test(
    name = "paired_test",
    size = "enormous",
    timeout = "eternal",
    srcs = ["run.py"],
    args = [
        "$(rootpath //test/benchmarks/network:httpd_test)",
        "$(rootpath benchstat)",
    ],
    data = [
        "benchstat",
        "inputs.json",
        "//test/benchmarks/network:httpd_test",
    ],
    local = True,
    main = "run.py",
    tags = ["manual"],
)
