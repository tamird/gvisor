"""Tools for testing YAML files against schemas."""

load("@bazel_skylib//lib:shell.bzl", "shell")
load("@bazel_skylib//rules:native_binary.bzl", "native_test")

def _yaml_test_inputs_impl(ctx):
    args = [
        "-schema=" + ctx.file.schema.short_path,
        "-strict=" + ("true" if ctx.attr.strict else "false"),
        "-disallow_comments=" + ("true" if ctx.attr.disallow_comments else "false"),
        "--",
    ] + [src.short_path for src in ctx.files.srcs]

    # A macro cannot resolve selects or expand a filegroup. Pass the configured
    # arguments to native_test as a Make variable, which Bazel expands
    # recursively and tokenizes before it starts the validator.
    argv = " ".join([shell.quote(arg.replace("$", "$$")) for arg in args])
    return [
        DefaultInfo(files = depset([ctx.file.schema] + ctx.files.srcs)),
        platform_common.TemplateVariableInfo({"YAMLTEST_ARGS": argv}),
    ]

_yaml_test_inputs = rule(
    implementation = _yaml_test_inputs_impl,
    attrs = {
        "srcs": attr.label_list(allow_files = True, mandatory = True),
        "schema": attr.label(allow_single_file = True, mandatory = True),
        "strict": attr.bool(mandatory = True),
        "disallow_comments": attr.bool(mandatory = True),
    },
)

def yaml_test(name, srcs, schema, strict = True, disallow_comments = False, **kwargs):
    """Tests YAML files against a JSON schema.

    Args:
        name: Test target name.
        srcs: Input YAML files or filegroups.
        schema: JSON schema file.
        strict: Whether to use strict mode for YAML decoding.
        disallow_comments: Whether to disallow comments in the YAML files.
        **kwargs: Additional test attributes.
    """
    inputs = name + "_inputs"
    _yaml_test_inputs(
        name = inputs,
        srcs = srcs,
        schema = schema,
        strict = strict,
        disallow_comments = disallow_comments,
        tags = ["manual"],
        testonly = True,
        visibility = ["//visibility:private"],
    )
    native_test(
        name = name,
        src = Label("//tools/yamltest:yamltest"),
        out = name,
        args = ["$(YAMLTEST_ARGS)"] + kwargs.pop("args", []),
        data = [":" + inputs] + kwargs.pop("data", []),
        toolchains = [":" + inputs] + kwargs.pop("toolchains", []),
        **kwargs
    )
