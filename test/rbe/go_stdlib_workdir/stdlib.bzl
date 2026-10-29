"""Expose the actual standard library outputs in release configurations."""

load("@io_bazel_rules_go//go/private:providers.bzl", "GoStdLib")
load("//tools:arch.bzl", "arch_transition", "transition_allowlist")

def _stdlib_outputs_impl(ctx):
    libraries = [ctx.attr.tool[GoStdLib]] + [
        dependency[GoStdLib]
        for dependency in ctx.split_attr.stdlib.values()
    ]
    return [DefaultInfo(files = depset(
        direct = [library._list_json for library in libraries],
        transitive = [library.libs for library in libraries],
    ))]

stdlib_outputs = rule(
    implementation = _stdlib_outputs_impl,
    attrs = {
        "stdlib": attr.label(default = "@io_bazel_rules_go//:stdlib", cfg = arch_transition),
        "tool": attr.label(default = "@io_bazel_rules_go//:stdlib", cfg = "exec"),
        "_allowlist_function_transition": attr.label(default = transition_allowlist),
    },
)
