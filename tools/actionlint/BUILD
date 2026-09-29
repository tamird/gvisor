package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

alias(
    name = "actionlint",
    actual = select({
        "@platforms//cpu:aarch64": "@actionlint_linux_arm64//:actionlint",
        "@platforms//cpu:x86_64": "@actionlint_linux_amd64//:actionlint",
    }),
)
