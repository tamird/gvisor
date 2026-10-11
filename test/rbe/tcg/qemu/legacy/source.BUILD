exports_files([
    "VERSION",
    "qemu-source.json",
])

filegroup(
    name = "sources",
    srcs = glob(
        ["**"],
        exclude = ["BUILD.bazel"],
    ),
    visibility = ["//visibility:public"],
)
