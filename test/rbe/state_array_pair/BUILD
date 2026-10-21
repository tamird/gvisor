load("@rules_python//python:py_test.bzl", "py_test")

# Diagnostic-only input files are staged from the two source-bound builds.
py_test(
    name = "paired_test",
    size = "large",
    timeout = "long",
    srcs = ["run.py"],
    main = "run.py",
    args = ["$(rootpath @org_golang_x_perf//cmd/benchstat)"],
    data = [
        "inputs.json",
        "arms/before/_main/pkg/state/tests/benchmark/benchmark_test",
        "arms/after/_main/pkg/state/tests/benchmark/benchmark_test",
        "@org_golang_x_perf//cmd/benchstat",
    ],
    tags = ["manual"],
)
