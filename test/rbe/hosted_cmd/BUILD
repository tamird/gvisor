load("@rules_shell//shell:sh_test.bzl", "sh_test")

sh_test(
    name = "original",
    size = "large",
    srcs = ["run.sh"],
    args = [
        "$(location :hosted_cmd.py)",
        "$(location :cmd.tar)",
        "$(location :identity.json)",
    ],
    data = [
        "cmd.tar",
        "hosted_cmd.py",
        "identity.json",
    ],
)
