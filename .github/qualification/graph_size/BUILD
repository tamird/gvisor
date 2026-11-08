load("@rules_shell//shell:sh_test.bzl", "sh_test")

sh_test(
    name = "paired",
    size = "large",
    srcs = ["paired.sh"],
    args = [
        "$(rootpath :run.py)",
        "$(rootpath :stock.bin)",
        "$(rootpath :fixed.bin)",
        "$(rootpath :stock-shapes.bin)",
        "$(rootpath :fixed-shapes.bin)",
    ],
    data = [
        "fixed.bin",
        "fixed-shapes.bin",
        "run.py",
        "stock.bin",
        "stock-shapes.bin",
    ],
    local = True,
    tags = ["manual"],
)
