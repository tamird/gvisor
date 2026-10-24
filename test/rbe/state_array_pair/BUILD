load("@rules_python//python:py_test.bzl", "py_test")

# The callback stages each source's executable and complete runfiles together.
py_test(
    name = "paired_test",
    size = "large",
    timeout = "long",
    srcs = ["run.py"],
    args = ["$(rootpath benchstat)"],
    data = [
        "benchstat",
        "inputs.json",
    ] + glob(["arms/**"]),
    local = True,
    main = "run.py",
    tags = ["manual"],
)
