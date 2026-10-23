load("@rules_python//python:py_test.bzl", "py_test")

# The callback stages these exact configured outputs before test analysis.
py_test(
    name = "paired_test",
    size = "large",
    timeout = "long",
    srcs = ["run.py"],
    main = "run.py",
    args = ["$(rootpath :benchstat)"],
    data = [
        "arms/before/_main/pkg/sentry/pgalloc/pgalloc_test",
        "arms/corrected/_main/pkg/sentry/pgalloc/pgalloc_test",
        "arms/reviewed/_main/pkg/sentry/pgalloc/pgalloc_test",
        "benchstat",
        "inputs.json",
    ],
    tags = ["manual"],
)
