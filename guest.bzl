def _tamago_guest_impl(ctx):
    elf = ctx.actions.declare_file(ctx.label.name + ".elf")
    args = ctx.actions.args()
    args.add(ctx.file._go)
    args.add(elf)
    if ctx.attr.fixed:
        args.add("--fixed")
    ctx.actions.run(
        executable = ctx.attr._driver[DefaultInfo].files_to_run,
        inputs = depset(transitive = [
            ctx.attr._sdk[DefaultInfo].files,
            ctx.attr._sources[DefaultInfo].files,
        ]),
        outputs = [elf],
        arguments = [args],
        mnemonic = "TamaGoGuest",
    )
    return [DefaultInfo(files = depset([elf]))]

tamago_guest = rule(
    implementation = _tamago_guest_impl,
    attrs = {
        "fixed": attr.bool(),
        "_driver": attr.label(default = "//:guest_compiler", executable = True, cfg = "exec"),
        "_go": attr.label(default = "@tamago_sdk//:bin/go", allow_single_file = True),
        "_sdk": attr.label(default = "@tamago_sdk//:files"),
        "_sources": attr.label(default = "//:guest_sources"),
    },
)
