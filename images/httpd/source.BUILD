package(default_visibility = ["//visibility:public"])

exports_files([
    "LICENSE",
    "NOTICE",
])

# This shared overlay also serves APR archives without HTTPD's runtime data.
exports_files(glob(
    [
        "docs/conf/mime.types",
        "docs/docroot/index.html",
    ],
    allow_empty = True,
))

filegroup(
    name = "source",
    srcs = glob(["**"], exclude = ["BUILD.bazel"]),
)
