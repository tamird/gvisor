"""Expose the existing go-list output beside a normal product package build."""

load("@io_bazel_rules_go//go/private:providers.bzl", "GoStdLib")

def _metadata_impl(ctx):
    return [DefaultInfo(files = depset([ctx.attr.stdlib[GoStdLib]._list_json]))]

stdlib_metadata = rule(
    implementation = _metadata_impl,
    attrs = {"stdlib": attr.label(default = "@io_bazel_rules_go//:stdlib", providers = [GoStdLib])},
)
