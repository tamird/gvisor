load(":outputs.bzl", "root_probe", "stdlib_outputs")

stdlib_outputs(name = "stdlib")

# The normal Go library exposes its export archive by default. Select the
# existing link archive as well so its body can be joined to GoCompilePkg.
filegroup(
    name = "gohacks_archive",
    srcs = ["//pkg/gohacks"],
    output_group = "libs",
)

root_probe(
    name = "root",
    tool = select({
        "@platforms//cpu:aarch64": "@bootstrap_busybox_arm64//:payload/usr/bin/busybox",
        "@platforms//cpu:x86_64": "@bootstrap_busybox//:payload/usr/bin/busybox",
    }),
)
