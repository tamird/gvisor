load("@rules_python//python:py_test.bzl", "py_test")

py_test(
    name = "serving_test",
    size = "enormous",
    timeout = "eternal",
    srcs = ["run.py"],
    args = [
        "$(rootpath //test/benchmarks/network:nginx_test)",
        "$(rootpath benchstat)",
    ],
    data = [
        "benchstat",
        "inputs.json",
        "//test/benchmarks/network:nginx_test",
    ],
    local = True,
    main = "run.py",
    tags = ["manual"],
)

py_test(
    name = "lifecycle_test",
    size = "enormous",
    timeout = "eternal",
    srcs = ["run.py"],
    args = [
        "$(rootpath //test/benchmarks/base:startup_test)",
        "$(rootpath benchstat)",
    ],
    data = [
        "benchstat",
        "inputs.json",
        "//test/benchmarks/base:startup_test",
    ],
    local = True,
    main = "run.py",
    tags = ["manual"],
)

py_test(
    name = "size_test",
    size = "enormous",
    timeout = "eternal",
    srcs = ["run.py"],
    args = [
        "$(rootpath //test/benchmarks/base:size_test)",
        "$(rootpath benchstat)",
    ],
    data = [
        "benchstat",
        "inputs.json",
        "//test/benchmarks/base:size_test",
    ],
    local = True,
    main = "run.py",
    tags = ["manual"],
)

py_test(
    name = "usage_test",
    size = "enormous",
    timeout = "eternal",
    srcs = ["run.py"],
    args = [
        "$(rootpath //test/benchmarks/base:usage_test)",
        "$(rootpath benchstat)",
    ],
    data = [
        "benchstat",
        "inputs.json",
        "//test/benchmarks/base:usage_test",
    ],
    local = True,
    main = "run.py",
    tags = ["manual"],
)
