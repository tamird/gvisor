load("@bazel_skylib//rules:write_file.bzl", "write_file")
load("//tools:defs.bzl", "go_library")

write_file(
    name = "record_src",
    out = "record.go",
    content = [
        "package qualification_generated",
        "",
        "// +stateify savable",
        'type record struct { value int `state:".(v2.PCG)"` }',
    ],
)

write_file(
    name = "values_src",
    out = "values.go",
    content = [
        "package qualification_generated",
        "",
        'import ("context"; v2 "math/rand/v2")',
        "func (*record) saveValue() v2.PCG { return v2.PCG{} }",
        "func (*record) loadValue(context.Context, v2.PCG) {}",
    ],
)

go_library(
    name = "qualification_generated",
    srcs = [
        "record.go",
        "values.go",
    ],
)
