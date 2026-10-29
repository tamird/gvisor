load("@bazel_skylib//rules:write_file.bzl", "write_file")
load("//tools:defs.bzl", "go_binary")
load("//tools:release.bzl", "RELEASE_FILES")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

write_file(
    name = "members",
    out = "members.txt",
    content = sorted(RELEASE_FILES.values()),
)

go_binary(
    name = "stagedcheck",
    srcs = ["main.go"],
    embedsrcs = [":members"],
    pure = True,
    visibility = ["//visibility:public"],
)
