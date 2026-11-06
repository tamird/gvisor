load("@rules_shell//shell:sh_test.bzl", "sh_test")

sh_test(
    name = "guest",
    size = "medium",
    srcs = ["guest.py"],
    args = [
        "release",
        "$(rootpath :busybox)",
    ] + select({
        "//tools:gotsan": ["race"],
        "//conditions:default": ["normal"],
    }),
    data = [
        ":busybox",
        "//:release",
    ],
    local = True,
)
