load("@bazel_skylib//rules:write_file.bzl", "write_file")
load("//tools:defs.bzl", "go_library")

write_file(
    name = "generated",
    out = "generated.go",
    content = ["package qualification_unaliased"],
)

go_library(
    name = "qualification_unaliased",
    srcs = [
        "generated.go",
        "record.go",
        "values.go",
    ],
)
