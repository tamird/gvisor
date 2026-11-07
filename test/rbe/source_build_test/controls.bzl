"""Compare native default outputs with an explicit test dependency barrier."""

def _native_impl(ctx):
    out = ctx.actions.declare_file(ctx.label.name)
    target_inputs = depset(transitive = [target[DefaultInfo].files for target in ctx.attr.targets])
    ctx.actions.symlink(output = out, target_file = ctx.executable._runner, is_executable = True)
    runtime = [ctx.executable._runner]
    if ctx.attr.barrier:
        stamp = ctx.actions.declare_file(ctx.label.name + ".complete")
        ctx.actions.run(
            executable = ctx.toolchains["@bazel_lib//lib:coreutils_toolchain_type"].coreutils_info.bin,
            arguments = ["touch", stamp.path],
            inputs = target_inputs,
            outputs = [stamp],
            mnemonic = "BuildTestBarrier",
            toolchain = "@bazel_lib//lib:coreutils_toolchain_type",
        )
        runtime.append(stamp)
    return [DefaultInfo(
        executable = out,
        files = depset([out], transitive = [] if ctx.attr.barrier else [target_inputs]),
        runfiles = ctx.runfiles(files = runtime),
    )]

_build_test = rule(
    implementation = _native_impl,
    test = True,
    toolchains = ["@bazel_lib//lib:coreutils_toolchain_type"],
    attrs = {
        "targets": attr.label_list(mandatory = True),
        "barrier": attr.bool(),
        "_runner": attr.label(default = "//test/rbe/source_build_test:pass", cfg = "target", executable = True),
    },
)

def defaults_test(name, targets, **kwargs):
    _build_test(name = name, targets = targets, size = kwargs.pop("size", "small"), **kwargs)

def barrier_test(name, targets, **kwargs):
    _build_test(name = name, targets = targets, size = kwargs.pop("size", "small"), barrier = True, **kwargs)

def _input_impl(ctx):
    out = ctx.actions.declare_file(ctx.label.name + ".input")
    ctx.actions.run(
        executable = ctx.executable._probe,
        arguments = ["fail" if ctx.attr.fail else "mark", out.path],
        outputs = [out],
        mnemonic = "BuildTestFixture",
        toolchain = None,
    )
    return [DefaultInfo(files = depset([out]))]

dependency = rule(
    implementation = _input_impl,
    attrs = {
        "fail": attr.bool(),
        "_probe": attr.label(default = "//test/rbe/source_build_test:probe", cfg = "exec", executable = True),
    },
)

def _observe_impl(ctx):
    # Only project the original executable here. Its separate Skylib marker
    # actions are submitted through their authentic generated labels.
    program = ctx.attr.program[DefaultInfo].files_to_run.executable
    out = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.run(
        executable = ctx.executable._probe,
        arguments = ["launch", program.path, out.path],
        inputs = [program],
        outputs = [out],
        mnemonic = "InspectBuildTestLauncher",
        toolchain = None,
    )
    return [DefaultInfo(files = depset([out]))]

observe_launcher = rule(
    implementation = _observe_impl,
    attrs = {
        "program": attr.label(mandatory = True),
        "_probe": attr.label(default = "//test/rbe/source_build_test:probe", cfg = "exec", executable = True),
    },
)
