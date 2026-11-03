load("@bazel_skylib//rules:write_file.bzl", "write_file")
load("//tools:defs.bzl", "go_library")

write_file(
    name = "generated",
    out = "generated.go",
    content = ["package qualification_imports"],
)

go_library(
    name = "qualification_imports",
    srcs = [
        "generated.go",
        "record.go",
        "values.go",
    ],
)
