"""Generics tests."""

load("//tools/bazeldefs:cgroup_test.bzl", "cgroup_v1_tags", "cgroup_v1_variant", "with_cgroup_v1")
load("//tools/bazeldefs:test_architectures.bzl", "test_architecture_tags", "test_architecture_variants", "with_test_architecture")
load("//tools/go_generics:defs.bzl", "go_template", "go_template_instance")

def _go_generics_test_impl(ctx):
    runner = ctx.actions.declare_file(ctx.label.name)
    runner_content = "\n".join([
        "#!/bin/bash",
        "exec diff --ignore-blank-lines --ignore-matching-lines=^[[:space:]]*// %s %s" % (
            ctx.files.template_output[0].short_path,
            ctx.files.expected_output[0].short_path,
        ),
        "",
    ])
    ctx.actions.write(runner, runner_content, is_executable = True)
    return [DefaultInfo(
        executable = runner,
        runfiles = ctx.runfiles(
            files = ctx.files.template_output + ctx.files.expected_output,
            collect_default = True,
            collect_data = True,
        ),
    )]

_go_generics_test = rule(
    implementation = _go_generics_test_impl,
    attrs = {
        "template_output": attr.label(mandatory = True, allow_single_file = True),
        "expected_output": attr.label(mandatory = True, allow_single_file = True),
    },
    test = True,
)

def _compile_go_generics_test(**kwargs):
    _go_generics_test(**kwargs)

# with_cfg requires the returned transition rule to be exported at module scope.
# buildifier: disable=unused-variable
_go_generics_amd64_test, _go_generics_amd64_transition = with_test_architecture(_compile_go_generics_test, "amd64").build()

# buildifier: disable=unused-variable
_go_generics_arm64_test, _go_generics_arm64_transition = with_test_architecture(_compile_go_generics_test, "arm64").build()

# buildifier: disable=unused-variable
_go_generics_cgroup_v1_test, _go_generics_cgroup_v1_transition = with_cgroup_v1(_compile_go_generics_test)

def go_generics_test(name, inputs, output, types = None, consts = None, architectures = ["amd64", "arm64"], **kwargs):
    """Instantiates a generics test.

    Args:
        name: the name of the test.
        inputs: all the input files.
        output: the output files.
        types: the template types (dictionary).
        consts: the template consts (dictionary).
        architectures: Additional native architecture variants.
        **kwargs: additional arguments for the template_instance.
    """
    if types == None:
        types = dict()
    if consts == None:
        consts = dict()
    go_template(
        name = name + "_template",
        srcs = inputs,
        types = types.keys(),
        consts = consts.keys(),
    )
    go_template_instance(
        name = name + "_output",
        template = ":" + name + "_template",
        out = name + "_output.go",
        types = types,
        consts = consts,
        **kwargs
    )
    test_kwargs = {
        "template_output": name + "_output.go",
        "expected_output": output,
        "tags": cgroup_v1_tags(test_architecture_tags(architectures, [])),
    }
    _go_generics_test(name = name + "_test", **test_kwargs)
    cgroup_v1_variant(name + "_test", _go_generics_cgroup_v1_test, test_kwargs)
    test_architecture_variants(
        name + "_test",
        architectures,
        {"amd64": _go_generics_amd64_test, "arm64": _go_generics_arm64_test},
        test_kwargs,
    )
