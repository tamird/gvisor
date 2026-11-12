load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
load("//test/rbe/tcg:defs.bzl", "arm64_tcg_test")

sh_binary(
    name = "shared_payload",
    srcs = ["guest.sh"],
    data = glob(["inputs/**"]),
)

[
    arm64_tcg_test(
        name = name,
        payload = ":shared_payload",
        image = image,
        size = "medium",
        timeout = "moderate",
        tags = ["manual"],
    )
    for name, image in [
        ("page4k", "//test/rbe/tcg:rc_guest"),
        ("page64k", "//test/rbe/tcg:guest"),
    ]
]
