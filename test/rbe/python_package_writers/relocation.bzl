"""Run a real package writer with its executable and runfiles relocated."""

def _relocated_writer_impl(ctx):
    runtime = ctx.toolchains["@bazel_tools//tools/python:toolchain_type"].py3_runtime
    writer = ctx.attr.writer[DefaultInfo].files_to_run
    out = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.run(
        executable = runtime.interpreter,
        arguments = [ctx.file.script.path, writer.executable.path, ctx.file.seed.path, out.path],
        inputs = depset([ctx.file.script, ctx.file.seed], transitive = [runtime.files]),
        tools = [writer],
        outputs = [out],
        mnemonic = "RelocatedPythonWriter",
    )
    return [DefaultInfo(files = depset([out]))]

relocated_writer = rule(
    implementation = _relocated_writer_impl,
    attrs = {
        "script": attr.label(mandatory = True, allow_single_file = True),
        "seed": attr.label(mandatory = True, allow_single_file = True),
        "writer": attr.label(mandatory = True, executable = True, cfg = "exec"),
    },
    toolchains = ["@bazel_tools//tools/python:toolchain_type"],
)
