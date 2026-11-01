def _exec_tool_impl(ctx):
    return [DefaultInfo(files = depset([ctx.executable.tool]))]

exec_tool = rule(
    implementation = _exec_tool_impl,
    attrs = {"tool": attr.label(executable = True, cfg = "exec", mandatory = True)},
)
