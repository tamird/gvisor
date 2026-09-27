package(default_visibility = ["//visibility:public"])

exports_files(["COPYING"])

filegroup(
    name = "sources",
    srcs = glob(
        ["**"],
        exclude = [
            "BUILD",
            "BUILD.bazel",
        ],
    ),
)
