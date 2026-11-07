load("@rules_shell//shell:sh_test.bzl", "sh_test")

[
    sh_test(
        name = size,
        size = size,
        srcs = ["observe.sh"],
        tags = ["manual"],
    )
    for size in ["medium", "large"]
]
