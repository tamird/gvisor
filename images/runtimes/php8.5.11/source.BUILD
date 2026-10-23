load("@rules_pkg//pkg:tar.bzl", "pkg_tar")

# PHPT tests use companion files throughout the source tree. Preserve their
# relative paths and executable bits, without configuring or compiling PHP.
pkg_tar(
    name = "fixtures",
    srcs = glob(
        ["**"],
        exclude = [
            "BUILD.bazel",
            "REPO.bazel",
            "WORKSPACE",
            "WORKSPACE.bazel",
        ],
    ),
    mode = "",
    package_dir = "root/php-8.5.11",
    strip_prefix = ".",
    visibility = ["//visibility:public"],
)
