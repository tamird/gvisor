"""Stage the release packages in the architecture directories used by the publisher."""

load("//tools:arch.bzl", "arch_transition")

_RELEASE_DIR = {"amd64": "x86_64", "arm64": "aarch64"}

def _stage(ctx, arch, src):
    path = "%s/%s/%s" % (ctx.label.name, arch, src.basename)
    out = ctx.actions.declare_file(path)
    ctx.actions.symlink(output = out, target_file = src)
    return out

def _release_artifacts_impl(ctx):
    outputs = []
    for arch, targets in ctx.split_attr.srcs.items():
        for target in targets:
            for file in target[DefaultInfo].files.to_list():
                outputs.append(_stage(ctx, _RELEASE_DIR[arch], file))
    return [DefaultInfo(files = depset(outputs))]

release_artifacts = rule(
    implementation = _release_artifacts_impl,
    attrs = {
        "srcs": attr.label_list(cfg = arch_transition, mandatory = True),
    },
)

def _release_inputs_impl(ctx):
    files = []
    required = []
    for arch, package in ctx.split_attr.package.items():
        directory = _RELEASE_DIR[arch]
        (deb,) = package[DefaultInfo].files.to_list()
        path = "%s/%s" % (directory, deb.basename)
        required.append(path)
        files.extend([deb, _stage(ctx, directory, deb)])
        for target in ctx.split_attr.downloads[arch]:
            for src in target[DefaultInfo].files.to_list():
                path = "%s/%s" % (directory, src.basename)
                required.append(path)
                out = ctx.actions.declare_file(ctx.label.name + "/" + path + ".sha512")
                _run_stage(ctx, "checksum", src, out)
                files.extend([src, _stage(ctx, directory, src), out])
    python = ctx.actions.declare_directory(ctx.label.name + "/python")
    if ctx.file.python_dist:
        _run_stage(ctx, "python", ctx.file.python_dist, python)
    else:
        # A non-Python build replaces any previously materialized wheel tree.
        ctx.actions.run(
            executable = ctx.executable._stager,
            arguments = ["empty", python.path],
            outputs = [python],
            mnemonic = "EmptyReleaseDist",
            toolchain = None,
        )
    files.append(python)
    layout = ctx.actions.declare_file(ctx.label.name + "/inputs")
    ctx.actions.write(layout, "\000".join(required) + "\000")
    return [DefaultInfo(files = depset(files + [layout]))]

def _run_stage(ctx, command, src, out):
    ctx.actions.run(
        executable = ctx.executable._stager,
        arguments = [command, src.path, out.path],
        inputs = [src],
        outputs = [out],
        mnemonic = "ReleaseSHA512",
        progress_message = "Preparing %{input} for the release",
        toolchain = None,
    )

release_inputs = rule(
    implementation = _release_inputs_impl,
    attrs = {
        "downloads": attr.label_list(cfg = arch_transition, mandatory = True),
        "package": attr.label(cfg = arch_transition, mandatory = True),
        "python_dist": attr.label(allow_single_file = True),
        "_stager": attr.label(default = "//tools/release:stage", cfg = "exec", executable = True),
    },
    doc = "Build the unsigned, checksummed release from both architecture configurations.",
)
