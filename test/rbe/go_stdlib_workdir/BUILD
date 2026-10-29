load("//test/release:defs.bzl", "release_artifacts")
load(":stdlib.bzl", "stdlib_outputs")

stdlib_outputs(name = "configured_stdlibs")

release_artifacts(
    name = "archives",
    srcs = [":configured_stdlibs"],
    tags = ["manual"],
)
