"""Analysis-only probe of declared first-party formatter inputs."""

_REPOSITORY = Label("//:license").workspace_name
_CONFIG_NAMES = (".clang-format", "_clang-format", ".clang-format-ignore")

def _coverage_impl(target, ctx):
    if ctx.rule == None or ctx.label.workspace_name != _REPOSITORY:
        return []
    sources = {}
    configs = {}
    for attribute in dir(ctx.rule.files):
        for source in getattr(ctx.rule.files, attribute):
            if not source.is_source or source.owner.workspace_name != _REPOSITORY:
                continue
            if source.extension in ("c", "cc", "h"):
                sources[source.path] = source
            elif source.basename in _CONFIG_NAMES:
                configs[source.path] = source
    if not sources:
        return []
    configs[ctx.file._root_style.path] = ctx.file._root_style
    marker = ctx.actions.declare_file(target.label.name + ".clang_format_coverage")
    ctx.actions.run_shell(
        inputs = list(sources.values()) + list(configs.values()),
        outputs = [marker],
        arguments = [marker.path],
        command = ': > "$1"',
        mnemonic = "ClangFormatCoverage",
    )
    return [OutputGroupInfo(clang_format_coverage = depset([marker]))]

clang_format_coverage = aspect(
    implementation = _coverage_impl,
    attr_aspects = ["*"],
    attrs = {"_root_style": attr.label(default = Label("//:.clang-format"), allow_single_file = True)},
)
