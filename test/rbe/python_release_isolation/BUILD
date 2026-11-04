load("@rules_shell//shell:sh_test.bzl", "sh_test")

sh_test(
    name = "compare",
    srcs = ["run.sh"],
    data = [
        "compare.py",
        "stock.tar",
        "fixed.tar",
    ],
    size = "small",
    tags = ["local"],
    timeout = "moderate",
)
