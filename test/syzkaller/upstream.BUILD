package(default_visibility = ["//visibility:public"])

exports_files([
    "go.mod",
    "go.sum",
])

filegroup(
    name = "sources",
    srcs = glob(
        ["**"],
        exclude = ["BUILD.bazel"],
    ),
)
