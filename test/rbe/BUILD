load("@rules_python//python:py_test.bzl", "py_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

py_test(
    name = "image_execution_test",
    size = "small",
    srcs = [
        "image_execution.py",
        "image_execution_test.py",
    ],
    data = ["image_execution_fixture.json"],
    imports = ["."],
)
