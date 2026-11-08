def _native_result(ctx, success, message):
    report = ctx.actions.declare_file(ctx.label.name + ".report")
    ctx.actions.write(report, "" if success else message + "\n")
    ctx.actions.symlink(
        output = ctx.outputs.executable,
        target_file = ctx.executable._reader,
        is_executable = True,
    )
    return [
        DefaultInfo(
            executable = ctx.outputs.executable,
            runfiles = ctx.runfiles(files = [report]).merge(ctx.attr._reader[DefaultInfo].default_runfiles),
        ),
        RunEnvironmentInfo(environment = {"TEST_DEPS_REPORT": report.short_path}),
    ]
