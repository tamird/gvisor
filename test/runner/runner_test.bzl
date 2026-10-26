"""Raw syscall runner rule, exported before its configuration extension."""

def _runner_test_impl(ctx):
    # Generate a runner binary.
    runner = ctx.actions.declare_file(ctx.label.name)
    setup = ""
    if ctx.attr.requires_atime:
        setup = "%s --require-atime " % ctx.executable._setup_container.short_path
    runner_content = "\n".join([
        "#!/bin/bash",
        "set -euf -x -o pipefail",
        "if [[ -n \"${TEST_UNDECLARED_OUTPUTS_DIR}\" ]]; then",
        "  mkdir -p \"${TEST_UNDECLARED_OUTPUTS_DIR}\"",
        "  chmod a+rwx \"${TEST_UNDECLARED_OUTPUTS_DIR}\"",
        "fi",
        "exec %s%s %s \"$@\" %s\n" % (
            setup,
            ctx.files.runner[0].short_path,
            " ".join(ctx.attr.runner_args),
            ctx.files.test[0].short_path,
        ),
    ])
    ctx.actions.write(runner, runner_content, is_executable = True)

    # Return with all transitive files.
    runfiles = ctx.runfiles(
        transitive_files = depset(transitive = [
            target.data_runfiles.files
            for target in (ctx.attr.runner, ctx.attr.test)
            if hasattr(target, "data_runfiles")
        ]),
        files = ctx.files.runner + ctx.files.test,
        collect_default = True,
        collect_data = True,
    )
    if ctx.attr.requires_atime:
        runfiles = runfiles.merge(ctx.attr._setup_container[DefaultInfo].default_runfiles)
        runfiles = runfiles.merge(ctx.runfiles(files = [ctx.executable._setup_container]))
    return [
        DefaultInfo(executable = runner, runfiles = runfiles),
        testing.ExecutionInfo(ctx.attr.execution_requirements),
    ]

runner_test = rule(
    attrs = {
        "runner": attr.label(
            default = "//test/runner:runner",
        ),
        "test": attr.label(
            mandatory = True,
        ),
        "runner_args": attr.string_list(),
        "requires_atime": attr.bool(
            doc = "Enable host-backed atime updates; requires CAP_SYS_ADMIN on noatime mounts.",
        ),
        "_setup_container": attr.label(
            default = "//test/runner/setup_container",
            executable = True,
            cfg = "target",
        ),
        "data": attr.label_list(
            allow_files = True,
        ),
        "execution_requirements": attr.string_dict(
            doc = "Additional TestRunner execution requirements.",
        ),
    },
    test = True,
    implementation = _runner_test_impl,
)
