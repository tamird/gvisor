load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
load("//test/rbe/tcg:defs.bzl", "arm64_tcg_test")

sh_binary(
    name = "compare",
    testonly = True,
    srcs = ["guest.sh"],
    data = [
        "fixed",
        "stock",
    ],
)

arm64_tcg_test(
    name = "on_64k",
    size = "large",
    payload = ":compare",
    tags = [],
)
