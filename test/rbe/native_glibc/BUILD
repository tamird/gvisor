load(":loader.bzl", "glibc_loader")

glibc_loader(
    name = "loader",
    src = select({
        "@platforms//cpu:x86_64": "@qualification_glibc_amd64//:lib/x86_64-linux-gnu/ld-linux-x86-64.so.2",
        "@platforms//cpu:aarch64": "@qualification_glibc_arm64//:lib/aarch64-linux-gnu/ld-linux-aarch64.so.1",
    }),
    libraries = select({
        "@platforms//cpu:x86_64": "@qualification_glibc_amd64//:libraries",
        "@platforms//cpu:aarch64": "@qualification_glibc_arm64//:libraries",
    }),
    visibility = ["//visibility:public"],
)
