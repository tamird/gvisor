load("@rules_python//python:defs.bzl", "py_test")

py_test(
    name = "image_test",
    srcs = ["image_test.py"],
    args = ["$(location //test/docker:nodejs_source_amd64)"],
    data = [
        "node22-original.js",
        "probe.js",
        "//test/docker:nodejs_source_amd64",
    ],
    legacy_create_init = False,
    timeout = "long",
)
