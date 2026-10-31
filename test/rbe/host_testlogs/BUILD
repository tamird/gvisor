load("@rules_shell//shell:sh_test.bzl", "sh_test")

sh_test(
    name = "pass",
    srcs = ["case.sh"],
    args = ["pass"],
)

sh_test(
    name = "fail",
    srcs = ["case.sh"],
    args = ["fail"],
)

sh_test(
    name = "flaky",
    srcs = ["case.sh"],
    args = ["flaky"],
)
