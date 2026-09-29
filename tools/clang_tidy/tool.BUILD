package(default_visibility = ["//visibility:public"])

exports_files(["clang_tidy/data/bin/clang-tidy"])

# Keep the executable in its original layout beside its resource headers and
# any wheel-bundled shared libraries.
filegroup(
    name = "distribution",
    srcs = glob(["**"]),
)
