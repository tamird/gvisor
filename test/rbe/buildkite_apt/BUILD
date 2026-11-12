load("@rules_shell//shell:sh_test.bzl", "sh_test")

sh_test(
    name = "apt_lock_test",
    srcs = ["apt_lock_test.py"],
    data = ["candidate-hook", "stock-hook"],
    size = "medium",
)
