"""GitHub Actions checks with a declared actionlint binary and workflow inputs."""

load("@bazel_skylib//lib:shell.bzl", "shell")

def _actionlint_test_impl(ctx):
    if not ctx.files.srcs:
        fail("actionlint requires at least one workflow")
    marker = ctx.actions.declare_file(ctx.label.name + ".git-marker")
    ctx.actions.write(marker, "")
    runner = ctx.actions.declare_file(ctx.label.name)
    ctx.actions.write(
        runner,
        "#!/bin/bash\nexec %s -no-color -oneline -shellcheck= -pyflakes= %s\n" % (
            shell.quote(ctx.executable._tool.short_path),
            " ".join([shell.quote(f.short_path) for f in ctx.files.srcs]),
        ),
        is_executable = True,
    )
    return [DefaultInfo(
        executable = runner,
        runfiles = ctx.runfiles(
            files = [ctx.executable._tool] + ctx.files.srcs + ctx.files.data,
            # actionlint requires this marker to discover project configuration
            # and local actions. It does not inspect Git history or run Git.
            symlinks = {".git": marker},
        ),
    )]

actionlint_test = rule(
    implementation = _actionlint_test_impl,
    doc = "Checks GitHub workflows and their declared local metadata with actionlint.",
    attrs = {
        "srcs": attr.label_list(allow_files = [".yaml", ".yml"], mandatory = True),
        "data": attr.label_list(allow_files = True),
        "_tool": attr.label(
            default = Label("//tools/actionlint:actionlint"),
            allow_single_file = True,
            executable = True,
            cfg = "target",
        ),
    },
    test = True,
)
