load("//images:img.bzl", "image_outputs")
load("//tools:arch.bzl", "arch_genrule")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

filegroup(
    name = "sources",
    srcs = glob(["**"]),
    visibility = ["//images:__pkg__"],
)

# Loading the pulled manifest directly preserves its complete configuration.
alias(
    name = "postgresql",
    actual = select({
        "@platforms//cpu:aarch64": "@test_postgresql_arm64",
        "@platforms//cpu:x86_64": "@test_postgresql_amd64",
    }),
    tags = ["manual"],
)

image_outputs(
    name = "postgresql",
    image = ":postgresql",
    image_name = "compatibility/postgresql/postgresql",
)

arch_genrule(
    name = "qualification_postgresql_archives",
    src = ":postgresql_archive",
    template = "qualification_postgresql_archive_%s.tar",
)

arch_genrule(
    name = "qualification_postgresql_contexts",
    src = ":postgresql_context",
    template = "qualification_postgresql_context_%s.tar",
)
