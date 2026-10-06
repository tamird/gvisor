"""Tools for testing yaml files against schemas."""

load("//tools/bazeldefs:cgroup_test.bzl", "cgroup_v1_tags", "cgroup_v1_variant", "with_cgroup_v1")
load("//tools/bazeldefs:test_architectures.bzl", "test_architecture_tags", "test_architecture_variants", "with_test_architecture")

def _yaml_test_impl(ctx):
    """Implementation for yaml_test."""
    runner = ctx.actions.declare_file(ctx.label.name)
    ctx.actions.write(runner, "\n".join([
        "#!/bin/bash",
        "set -euo pipefail",
        "%s '-schema=%s' -strict=%s -disallow_comments=%s -- %s" % (
            ctx.files._tool[0].short_path,
            ctx.files.schema[0].short_path,
            "true" if ctx.attr.strict else "false",
            "true" if ctx.attr.disallow_comments else "false",
            " ".join([f.short_path for f in ctx.files.srcs]),
        ),
    ]), is_executable = True)
    return [DefaultInfo(
        runfiles = ctx.runfiles(files = ctx.files._tool + ctx.files.schema + ctx.files.srcs),
        executable = runner,
    )]

_yaml_test = rule(
    implementation = _yaml_test_impl,
    doc = "Tests a yaml file against a schema.",
    attrs = {
        "srcs": attr.label_list(
            doc = "The input yaml files.",
            mandatory = True,
            allow_files = True,
        ),
        "schema": attr.label(
            doc = "The schema file in JSON schema format.",
            allow_single_file = True,
            mandatory = True,
        ),
        "strict": attr.bool(
            doc = "Whether to use strict mode for YAML decoding.",
            mandatory = False,
            default = True,
        ),
        "disallow_comments": attr.bool(
            doc = "Whether to disallow comments in the YAML file.",
            mandatory = False,
            default = False,
        ),
        "_tool": attr.label(
            executable = True,
            cfg = "target",
            default = Label("//tools/yamltest:yamltest"),
        ),
    },
    test = True,
)

def _compile_yaml_test(**kwargs):
    _yaml_test(**kwargs)

# with_cfg requires the returned transition rule to be exported at module scope.
# buildifier: disable=unused-variable
_yaml_amd64_test, _yaml_amd64_transition = with_test_architecture(_compile_yaml_test, "amd64").build()
# buildifier: disable=unused-variable
_yaml_arm64_test, _yaml_arm64_transition = with_test_architecture(_compile_yaml_test, "arm64").build()
# buildifier: disable=unused-variable
_yaml_test_cgroup_v1_test, _yaml_test_cgroup_v1_transition = with_cgroup_v1(_compile_yaml_test)

def yaml_test(name, architectures = ["amd64", "arm64"], **kwargs):
    """Declares the original check and manual architecture/cgroup variants.

    Args:
        name: Original test target name.
        architectures: Additional native architecture variants.
        **kwargs: Attributes forwarded to the underlying test rule.
    """
    kwargs["tags"] = cgroup_v1_tags(test_architecture_tags(architectures, kwargs.get("tags", [])))
    _yaml_test(name = name, **kwargs)
    cgroup_v1_variant(name, _yaml_test_cgroup_v1_test, kwargs)
    test_architecture_variants(
        name,
        architectures,
        {"amd64": _yaml_amd64_test, "arm64": _yaml_arm64_test},
        kwargs,
    )
