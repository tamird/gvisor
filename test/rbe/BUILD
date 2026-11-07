load("@rules_shell//shell:sh_test.bzl", "sh_test")

sh_test(
    name = "cross_built_release_guest",
    size = "medium",
    srcs = ["cross_release_guest.sh"],
    data = [
        "cross_release_guest.py",
        "native_cross_inputs.py",
        "payload/busybox",
        "payload/manifest.json",
        "payload/release.tar.gz",
        "runtime_cross.py",
        "runtime_guest.py",
    ],
    local = True,
    tags = ["manual"],
)
