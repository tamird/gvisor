load("@go_host_compatible_sdk_label//:defs.bzl", "HOST_COMPATIBLE_SDK")

filegroup(
    name = "helpers",
    srcs = [
        HOST_COMPATIBLE_SDK.same_package_label("builder"),
        HOST_COMPATIBLE_SDK.same_package_label("pack.exe"),
    ],
    visibility = ["//visibility:public"],
)
