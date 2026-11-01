"""Expose the declared GNU loader as a native Bazel run-under executable."""

def _loader(ctx):
    executable = ctx.actions.declare_file(ctx.label.name)
    ctx.actions.symlink(output = executable, target_file = ctx.file.src, is_executable = True)
    return [DefaultInfo(
        executable = executable,
        runfiles = ctx.runfiles(
            files = [executable] + ctx.files.libraries,
            symlinks = {"test/rbe/native_glibc/lib/" + f.basename: f for f in ctx.files.libraries},
        ),
    )]

glibc_loader = rule(
    implementation = _loader,
    attrs = {
        "src": attr.label(allow_single_file = True, mandatory = True),
        "libraries": attr.label(allow_files = True, mandatory = True),
    },
    executable = True,
)
