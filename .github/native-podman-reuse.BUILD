# Qualification-only input substitution for the unchanged Podman smoke test.
# The workflow verifies the source-built runtime archive before exposing it.
load("@rules_shell//shell:sh_test.bzl", "sh_test")

sh_test(
    name = "podman_test_arm64",
    size = "large",
    srcs = ["smoke.sh"],
    args = [
        "$(rootpath reused/release/runsc)",
        "$(rootpath reused/alpine.tar)",
        "gvisor.dev/images/basic/alpine:latest",
    ],
    data = glob(["reused/release/**"]) + ["reused/alpine.tar"],
    tags = ["manual"],
    target_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:aarch64",
    ],
)
