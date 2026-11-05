"""Run the declared target-architecture observer on the selected remote worker."""

def _observe_worker_impl(ctx):
    out = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.run(
        executable = ctx.executable.observer,
        arguments = [out.path],
        outputs = [out],
        mnemonic = "ObserveWorkerRoot",
        execution_requirements = {"no-remote-cache": "1"},
        toolchain = None,
    )
    return [DefaultInfo(files = depset([out]))]

observe_worker = rule(
    implementation = _observe_worker_impl,
    attrs = {
        "observer": attr.label(executable = True, cfg = "target", mandatory = True),
    },
)
