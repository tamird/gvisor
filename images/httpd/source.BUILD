package(default_visibility = ["//visibility:public"])

# The three Apache release archives use the same source/notice boundary.
exports_files(glob([
    "LICENSE",
    "NOTICE",
    "docs/conf/mime.types",
    "docs/docroot/index.html",
]))

filegroup(
    name = "source",
    srcs = glob(["**"], exclude = ["BUILD.bazel"]),
)
