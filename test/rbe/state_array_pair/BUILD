load("@rules_python//python:py_test.bzl", "py_test")

# Diagnostic-only input files are staged from the two source-bound builds.
py_test(
    name = "paired_test",
    size = "large",
    timeout = "long",
    srcs = ["run.py"],
    args = ["$(rootpath :benchstat)"],
    data = [
        "arms/after/_main/pkg/state/tests/tests_test",
        "arms/before/_main/pkg/state/tests/tests_test",
        "benchstat",
        "inputs.json",
    ],
    main = "run.py",
    tags = [
        "manual",
        "qualification-output",
    ],
)
