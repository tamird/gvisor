"""Exploratory native formatter API checks."""

def _inventory_impl(ctx):
    files = depset(ctx.files.srcs)
    return [DefaultInfo(files = files, runfiles = ctx.runfiles(transitive_files = files))]

inventory = rule(
    implementation = _inventory_impl,
    attrs = {"srcs": attr.label_list(allow_files = True)},
)

def _check_impl(ctx):
    report = ctx.actions.declare_file(ctx.label.name + ".report")
    ctx.actions.run_shell(
        inputs = ctx.files.srcs,
        tools = [ctx.attr.checker[DefaultInfo].files_to_run],
        outputs = [report],
        arguments = [report.path, ctx.executable.checker.path] + [f.path for f in ctx.files.srcs],
        env = {"BUILD_WORKSPACE_DIRECTORY": "."},
        command = 'report="$1"; shift; "$@" > "$report" 2>&1 || { status=$?; cat "$report" >&2; exit "$status"; }',
        mnemonic = "NativeClangFormatPreflight",
    )
    return [DefaultInfo(files = depset([report]))]

check = rule(
    implementation = _check_impl,
    attrs = {
        "srcs": attr.label_list(allow_files = True),
        "checker": attr.label(executable = True, cfg = "exec"),
    },
)
