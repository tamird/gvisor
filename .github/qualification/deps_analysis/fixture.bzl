"""A declared validation and ordinary deps edge for the dependency aspect."""

def _vertex_impl(ctx):
    validation = ctx.actions.declare_file(ctx.label.name + ".validation")
    if ctx.attr.invalid:
        # This fixture action runs on the normal build host before the test child.
        ctx.actions.run_shell(
            outputs = [validation],
            command = "echo 'forbidden fixture validation reached' >&2; exit 1",
            mnemonic = "InvalidDepsTestFixture",
        )
    else:
        ctx.actions.write(validation, str(ctx.label))
    return [OutputGroupInfo(_validation = depset([validation]))]

vertex = rule(
    implementation = _vertex_impl,
    attrs = {"deps": attr.label_list(), "invalid": attr.bool()},
)
