"""Stateify is a tool for generating state wrappers for Go types."""

load("@io_bazel_rules_go//go:def.bzl", "GoInfo", "go_context", "new_go_info")
load("//tools/bazeldefs:go.bzl", "go_imports_action", "go_rule")

def _go_stateify_impl(ctx):
    """Type-checks and renders a selected package in one stateify action."""
    go = go_context(
        ctx,
        importpath = ctx.attr.importpath,
        importmap = ctx.attr.importmap,
        importpath_aliases = ctx.attr.importpath_aliases,
        embed = [],
    )

    # Reuse rules_go's package-source and dependency precedence. This private
    # view is checker input only; returning it would duplicate original files.
    checker = new_go_info(
        go,
        struct(
            srcs = ctx.attr.srcs,
            deps = ctx.attr.deps,
            embed = ctx.attr.checker_embed,
            cgo = ctx.attr.cgo,
            importpath_aliases = ctx.attr.importpath_aliases,
        ),
        include_package_metadata = False,
    )
    archives = {}
    archive_files = []
    canonical_archives = {}

    # Direct dependencies have the same precedence as the real compiler.
    # Transitives supply types referenced by those export records.
    direct = [archive.data for archive in checker.deps]
    transitive = depset(transitive = [archive.transitive for archive in checker.deps]).to_list()
    for records, is_direct in [(direct, True), (transitive, False)]:
        for data in records:
            export = data.export_file if data.export_file else data.file
            if data.importmap in canonical_archives and canonical_archives[data.importmap] != export.path:
                if is_direct:
                    fail("conflicting direct exports for compiler package %s" % data.importmap)
                continue
            canonical_archives[data.importmap] = export.path
            archive_files.append(export)
            entry = {"File": export.path, "ImportMap": data.importmap}
            for path in [data.importpath, data.importmap] + list(data.importpath_aliases):
                if path in archives and archives[path] != entry:
                    if is_direct:
                        fail("conflicting direct exports for import %s" % path)
                    continue
                archives[path] = entry
    group_inputs = {name: [] for name in ctx.attr.group_names}
    for target in ctx.attr.srcs:
        if target in ctx.attr.group_sources:
            group_inputs[ctx.attr.group_sources[target]].extend([f.path for f in target[DefaultInfo].files.to_list()])
    groups = []
    declaration_groups = []
    declaration_outputs = []
    cleaned_declarations = []
    raw_outputs = []
    for name, output in zip(ctx.attr.group_names, ctx.outputs.outs):
        raw = ctx.actions.declare_file(ctx.label.name + "/raw/" + output.basename)
        declaration = ctx.actions.declare_file(ctx.label.name + "/declarations/raw/" + output.basename)
        cleaned = ctx.actions.declare_file(ctx.label.name + "/declarations_" + output.basename)
        raw_outputs.append(raw)
        declaration_outputs.append(declaration)
        cleaned_declarations.append(cleaned)
        declaration_groups.append({"Sources": group_inputs[name], "Output": declaration.path})
        groups.append({"Sources": group_inputs[name], "Output": raw.path, "Declarations": cleaned.path})
        go_imports_action(ctx, declaration, cleaned)
    tags = list(go.mode.tags) + ctx.attr.gotags
    if go.mode.race:
        tags.append("race")
    if go.mode.msan:
        tags.append("msan")
    config_data = {
        "Package": ctx.attr.package,
        "ImportPath": ctx.attr.importpath,
        "StatePackage": "gvisor.dev/gvisor/pkg/state",
        "Imports": ctx.attr.imports,
        "Sources": [f.path for f in checker.srcs],
        "Groups": groups,
        "Archives": archives,
        "Stdlib": [f.path for f in go.stdlib.libs.to_list()],
        "GOOS": go.env["GOOS"],
        "GOARCH": go.env["GOARCH"],
        "Tags": tags,
        "GoVersion": "go" + go.sdk.version,
        "CgoEnabled": go.env["CGO_ENABLED"] == "1",
    }
    config = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.write(config, json.encode(config_data))
    declaration_config = ctx.actions.declare_file(ctx.label.name + "_declarations.json")
    config_data["Groups"] = declaration_groups
    ctx.actions.write(declaration_config, json.encode(config_data))
    ctx.actions.run(
        inputs = depset([declaration_config] + list(checker.srcs)),
        outputs = declaration_outputs,
        mnemonic = "GoStateifyDeclarations",
        progress_message = "Generating state declarations %s" % ctx.label,
        arguments = ["-config=" + declaration_config.path, "-declarations-only"],
        executable = ctx.executable._tool,
        env = go.env,
    )
    ctx.actions.run(
        inputs = depset([config] + list(checker.srcs) + archive_files + cleaned_declarations, transitive = [go.stdlib.libs]),
        outputs = raw_outputs,
        mnemonic = "GoStateify",
        progress_message = "Generating state methods %s" % ctx.label,
        arguments = ["-config=" + config.path],
        executable = ctx.executable._tool,
        env = go.env,
    )
    for raw, formatted in zip(raw_outputs, ctx.outputs.outs):
        go_imports_action(ctx, raw, formatted)
    generated = new_go_info(
        go,
        struct(importpath_aliases = ctx.attr.importpath_aliases),
        generated_srcs = ctx.outputs.outs,
        deps = [],
    )
    return [generated, DefaultInfo(files = depset(ctx.outputs.outs))]

# The action uses the selected target's source tags, word size and compiler
# exports. The generator itself runs in the execution configuration.
go_stateify = go_rule(
    rule,
    implementation = _go_stateify_impl,
    attrs = {
        "srcs": attr.label_list(allow_files = True, mandatory = True),
        "deps": attr.label_list(),
        "gotags": attr.string_list(),
        "group_sources": attr.label_keyed_string_dict(allow_files = True, mandatory = True),
        "checker_embed": attr.label_list(providers = [GoInfo]),
        "cgo": attr.bool(),
        "group_names": attr.string_list(mandatory = True),
        "outs": attr.output_list(mandatory = True),
        "imports": attr.string_list(),
        "package": attr.string(mandatory = True),
        "importpath": attr.string(mandatory = True),
        "importmap": attr.string(),
        "importpath_aliases": attr.string_list(),
        "_goimports": attr.label(
            default = "@org_golang_x_tools//cmd/goimports:goimports",
            executable = True,
            cfg = "exec",
        ),
        "_tool": attr.label(
            executable = True,
            cfg = "exec",
            default = Label("//tools/go_stateify:stateify"),
        ),
    },
)
