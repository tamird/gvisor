"""Stateify is a tool for generating state wrappers for Go types."""

load("//tools/bazeldefs:go.bzl", "go_context", "go_rule")

def _go_stateify_impl(ctx):
    """Implementation for the stateify tool."""
    go = go_context(ctx)
    output = ctx.outputs.out

    # Only the SDK's go command belongs on goimports' PATH.
    go_tool = ctx.actions.declare_file(ctx.label.name + "_tools/go")
    ctx.actions.symlink(output = go_tool, target_file = go.go, is_executable = True)

    # Run the stateify command.
    args = ["-go=%s" % go_tool.path, "-output=%s" % output.path]
    args.append("-fullpkg=%s" % ctx.attr.package)
    if ctx.attr._statepkg:
        args.append("-statepkg=%s" % ctx.attr._statepkg)
    if ctx.attr.imports:
        args.append("-imports=%s" % ",".join(ctx.attr.imports))
    args.append("--")
    for src in ctx.attr.srcs:
        args += [f.path for f in src.files.to_list()]
    ctx.actions.run(
        inputs = ctx.files.srcs,
        tools = [go_tool, go.runfiles],
        env = dict(go.env, GOPROXY = "off", GOTOOLCHAIN = "local", GOWORK = "off"),
        outputs = [output],
        mnemonic = "GoStateify",
        progress_message = "Generating state library %s" % ctx.label,
        arguments = args,
        executable = ctx.executable._tool,
    )

go_stateify = go_rule(
    rule,
    implementation = _go_stateify_impl,
    doc = "Generates save and restore logic from a set of Go files.",
    attrs = {
        "srcs": attr.label_list(
            doc = """
The input source files. These files should include all structs in the package
that need to be saved.
""",
            mandatory = True,
            allow_files = True,
        ),
        "imports": attr.string_list(
            doc = """
An optional list of extra non-aliased, Go-style absolute import paths required
for statified types.
""",
            mandatory = False,
        ),
        "package": attr.string(
            doc = "The fully qualified package name for the input sources.",
            mandatory = True,
        ),
        "out": attr.output(
            doc = "Name of the generator output file.",
            mandatory = True,
        ),
        "_tool": attr.label(
            executable = True,
            cfg = "exec",
            default = Label("//tools/go_stateify:stateify"),
        ),
        "_statepkg": attr.string(default = "gvisor.dev/gvisor/pkg/state"),
    },
)
