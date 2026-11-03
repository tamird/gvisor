load("@bazel_skylib//rules:write_file.bzl", "write_file")
load("//tools:defs.bzl", "go_library")

write_file(
    name = "generated",
    out = "generated.go",
    content = ["package qualification_declared"],
)

go_library(
    name = "qualification_declared",
    imports = [
        "math/rand/v2",
        "time",
    ],
    srcs = [
        "generated.go",
        "record.go",
        "values.go",
    ],
)
