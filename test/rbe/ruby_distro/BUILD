load("@rules_python//python:defs.bzl", "py_library")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

# Share the existing archive parser with the separate official-image experiment.
py_library(
    name = "image_archive",
    srcs = ["image_archive.py"],
    visibility = ["//test/rbe/ruby_official:__pkg__"],
)
