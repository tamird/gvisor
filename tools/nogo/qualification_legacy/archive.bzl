"""Go package shapes used to qualify the Nogo build adapter."""

load("@io_bazel_rules_go//go:def.bzl", "GoArchive")

LegacyPackage = provider(fields = ["importpath", "extra"])

def _go_wrap_cc(ctx):
    own = ctx.attr.compiled[GoArchive].data
    extra = ctx.attr.extra[GoArchive].data
    return [
        DefaultInfo(files = depset([own.export_file])),
        LegacyPackage(importpath = own.importpath, extra = [(extra.importpath, extra.file, extra.export_file)]),
    ]

go_wrap_cc = rule(implementation = _go_wrap_cc, attrs = {
    "compiled": attr.label(mandatory = True),
    "extra": attr.label(mandatory = True),
})

def _go_library(ctx):
    own = ctx.attr.compiled[GoArchive].data
    return [
        DefaultInfo(files = depset([own.file])),
        LegacyPackage(importpath = own.importpath, extra = []),
    ]

go_library = rule(implementation = _go_library, attrs = {
    "compiled": attr.label(mandatory = True),
    "deps": attr.label_list(),
    "srcs": attr.label_list(allow_files = [".go"]),
    "_go_context_data": attr.label(default = "@io_bazel_rules_go//:go_context_data"),
})
