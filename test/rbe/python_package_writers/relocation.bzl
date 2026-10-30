"""Run a real package writer with its executable and runfiles relocated."""

load("@hermetic_launcher//launcher:lib.bzl", "launcher")

def _native_zipapp_impl(ctx):
    runtime = ctx.toolchains["@bazel_tools//tools/python:toolchain_type"].py3_runtime
    archive = ctx.file.archive
    exe = ctx.actions.declare_file(ctx.label.name)
    args, transformed = launcher.args_from_entrypoint(runtime.interpreter)
    args.extend(["-I", "-S"])
    launcher.append_runfile(file = archive, embedded_args = args, transformed_args = transformed)
    launcher.compile_stub(ctx = ctx, embedded_args = args, transformed_args = transformed, output_file = exe)
    return [DefaultInfo(
        executable = exe,
        files = depset([exe]),
        runfiles = ctx.runfiles(files = [runtime.interpreter, archive], transitive_files = runtime.files),
    )]

native_zipapp = rule(
    implementation = _native_zipapp_impl,
    executable = True,
    attrs = {"archive": attr.label(allow_single_file = [".zip"], mandatory = True)},
    toolchains = [
        "@bazel_tools//tools/python:toolchain_type",
        launcher.finalizer_toolchain_type,
        launcher.template_toolchain_type,
    ],
)

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
