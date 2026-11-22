# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Declared C/C++ formatting reports and patches."""

# rules_lint 2.9.1 has no public counterparts for these private helpers.
# Revalidate this coupling when updating the pinned dependency.
# buildifier: disable=bzl-visibility
load("@aspect_rules_lint//lint/private:lint_aspect.bzl", "LintOptionsInfo", "output_files", "patch_and_output_files")

# buildifier: disable=bzl-visibility
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
        fail("Clang-format requires --@aspect_rules_lint//lint:fail_on_violation")
    sources = list(sources.values())
    snapshots = {}
    for config in configs.values():
        snapshot = ctx.actions.declare_file(target.label.name + "_clang_format_configs/" + config.short_path)
        ctx.actions.expand_template(template = config, output = snapshot, substitutions = {})
        snapshots[config.path] = snapshot
    inputs = sources + list(configs.values())
    patches = {}
    if options.fix:
        outputs, info = patch_and_output_files("AspectRulesLintClangFormat", target, ctx, files_to_lint = sources)
        for source, output in zip(sources, outputs):
            if "\n" in source.path or "\r" in source.path:
                fail("Unsupported formatter filename: " + repr(source.path))
            run_patcher(
                ctx,
                ctx.executable,
                inputs = [source] + list(configs.values()),
                args = ["-i", "--", source.path],
                files_to_diff = [source.path],
                patch_out = output.patch,
                tools = [ctx.executable._formatter],
                stdout = output.human.out,
                stderr = output.machine.out,
                patch_cfg_name = target.label.name + "_clang_format/" + source.short_path,
                mnemonic = "AspectRulesLintClangFormatPatch",
            )
            patches[source.path] = output.patch.path
    else:
        outputs, info = output_files("AspectRulesLintClangFormat", target, ctx)
        args = ctx.actions.args()
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
    manifest = ctx.actions.declare_file(target.label.name + ".clang_format_inputs.json")
    ctx.actions.write(manifest, json.encode({
        "sources": {source.path: patches.get(source.path) for source in sources},
        "configs": {path: snapshot.path for path, snapshot in snapshots.items()},
    }))
    groups = {name: getattr(info, name) for name in dir(info) if type(getattr(info, name)) == "depset"}
    groups["clang_format_inputs"] = depset([manifest] + snapshots.values())
    return [OutputGroupInfo(**groups)]

clang_format = aspect(
    implementation = _impl,
    attr_aspects = ["*"],
    attrs = patcher_attrs | {
        "_formatter": attr.label(default = Label("//tools/lint:clang_format_tool"), executable = True, cfg = "exec"),
        "_configs": attr.label_list(default = [Label("//:clang_format_config")]),
        "_options": attr.label(default = Label("@aspect_rules_lint//lint:options"), providers = [LintOptionsInfo]),
    },
)
