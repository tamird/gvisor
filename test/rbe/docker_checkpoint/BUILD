load("@rules_python//python:py_test.bzl", "py_test")

py_test(
    name = "carrier_test",
    size = "enormous",
    timeout = "eternal",
    srcs = ["carrier_test.py"],
    data = [
        "Dockerfile.qualification",
        "backport.patch",
        "content_probe.go",
        "source.json",
        "source.tar.gz",
    ],
    local = True,
    tags = [
        "manual",
        "qualification-output",
    ],
)
