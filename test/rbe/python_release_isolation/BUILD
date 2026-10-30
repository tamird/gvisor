load("@rules_shell//shell:sh_test.bzl", "sh_test")

sh_test(
    name = "compare",
    srcs = ["run.sh"],
    data = [
        "compare.py",
        "stock.sh",
        "fixed.sh",
        "//sandboxexec/sandbox/python:release_fixture_inputs",
    ],
    size = "small",
    tags = ["local"],
)
