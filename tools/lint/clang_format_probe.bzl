"""Bounded canonical report/patch integration probe."""

load("@aspect_rules_lint//lint/private:lint_aspect.bzl", "LintOptionsInfo", "output_files", "patch_and_output_files")
load("@aspect_rules_lint//lint/private:patcher_action.bzl", "patcher_attrs", "run_patcher")

_REPOSITORY = Label("//:license").workspace_name
_CONFIG_NAMES = (".clang-format", "_clang-format", ".clang-format-ignore")

def _impl(target, ctx):
    if ctx.rule == None or ctx.label.workspace_name != _REPOSITORY:
        return []
    sources = {}
    configs = {f.path: f for f in ctx.files._configs}
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
    options = ctx.attr._options[LintOptionsInfo]
    if not options.fail_on_violation:
        fail("Probe requires --@aspect_rules_lint//lint:fail_on_violation")
    sources = list(sources.values())
    inputs = sources + list(configs.values())
    args = ctx.actions.args()
    if options.fix:
        outputs, info = patch_and_output_files("AspectRulesLintClangFormat", target, ctx)
        args.add("-i")
        args.add("--")
        args.add_all(sources)
        run_patcher(
            ctx,
            ctx.executable,
            inputs = inputs,
            args = args,
            files_to_diff = [f.path for f in sources],
            patch_out = outputs.patch,
            tools = [ctx.executable._formatter],
            stdout = outputs.human.out,
            stderr = outputs.machine.out,
            mnemonic = "AspectRulesLintClangFormatPatch",
        )
    else:
        outputs, info = output_files("AspectRulesLintClangFormat", target, ctx)
        args.add_all(["--dry-run", "--Werror", "--"])
        args.add_all(sources)
        ctx.actions.run_shell(
            inputs = inputs,
            tools = [ctx.executable._formatter],
            outputs = [outputs.human.out],
            arguments = [outputs.human.out.path, ctx.executable._formatter.path, args],
            command = 'report="$1"; shift; "$@" > "$report" 2>&1 || { status=$?; cat "$report" >&2; exit "$status"; }',
            mnemonic = "AspectRulesLintClangFormatCheck",
        )
        ctx.actions.symlink(output = outputs.machine.out, target_file = outputs.human.out)
    return [info]

clang_format_probe = aspect(
    implementation = _impl,
    attr_aspects = ["*"],
    attrs = patcher_attrs | {
        "_formatter": attr.label(default = Label("//tools/lint:clang_format_probe_tool"), executable = True, cfg = "exec"),
        "_configs": attr.label_list(default = [Label("//:clang_format_probe_config")]),
        "_options": attr.label(default = Label("@aspect_rules_lint//lint:options"), providers = [LintOptionsInfo]),
    },
)
