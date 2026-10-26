licenses(["restricted"])

exports_files([
    "COPYING",
    "LICENSE",
    "VERSION",
])

filegroup(
    name = "sources",
    srcs = glob(
        ["**"],
        exclude = ["BUILD.bazel"],
    ),
    visibility = ["//visibility:public"],
)
