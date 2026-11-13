load("@rules_shell//shell:sh_test.bzl", "sh_test")

sh_test(
    name = "socket",
    size = "enormous",
    srcs = ["verify.sh"],
    data = ["cases.py"],
    tags = ["manual"],
)
