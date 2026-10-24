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
    name = "mariadb",
    actual = select({
        "@platforms//cpu:aarch64": "@test_mariadb_arm64",
        "@platforms//cpu:x86_64": "@test_mariadb_amd64",
    }),
    tags = ["manual"],
)

image_outputs(
    name = "mariadb",
    image = ":mariadb",
    image_name = "compatibility/mariadb/mariadb",
)

# Private qualification outputs; the product above is unchanged.
arch_genrule(
    name = "qualification_mariadb_archives",
    src = ":mariadb_archive",
    template = "qualification_mariadb_archive_%s.tar",
    visibility = ["//test/rbe/mariadb:__pkg__"],
)

arch_genrule(
    name = "qualification_mariadb_contexts",
    src = ":mariadb_context",
    template = "qualification_mariadb_context_%s.tar",
    visibility = ["//test/rbe/mariadb:__pkg__"],
)

alias(
    name = "qualification_mariadb_archive",
    actual = ":mariadb_archive",
    visibility = ["//test/compatibility:__pkg__"],
)
