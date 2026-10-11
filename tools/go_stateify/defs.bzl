"""Stateify is a tool for generating state wrappers for Go types."""

load("//tools/bazeldefs:go.bzl", "go_context", "go_rule", "go_transitive_archives")

def _go_stateify_impl(ctx):
    """Implementation for the stateify tool."""
    output = ctx.outputs.out

    # Run the stateify command.
    args = ["-output=%s" % output.path]
    args.append("-fullpkg=%s" % ctx.attr.package)
    if ctx.attr._statepkg:
        args.append("-statepkg=%s" % ctx.attr._statepkg)
    if ctx.attr.imports:
        args.append("-imports=%s" % ",".join(ctx.attr.imports))
    args.append("--")
    for src in ctx.attr.srcs:
        args += [f.path for f in src.files.to_list()]
    ctx.actions.run(
        inputs = ctx.files.srcs,
        outputs = [output],
        mnemonic = "GoStateify",
        progress_message = "Generating state library %s" % ctx.label,
        arguments = args,
        executable = ctx.executable._tool,
    )

go_stateify = rule(
    implementation = _go_stateify_impl,
    doc = "Generates save and restore logic from a set of Go files.",
    attrs = {
        "srcs": attr.label_list(
            doc = """
The input source files. These files should include all structs in the package
that need to be saved.
""",
            mandatory = True,
            allow_files = True,
        ),
        "imports": attr.string_list(
            doc = """
An optional list of extra non-aliased, Go-style absolute import paths required
for statified types.
""",
            mandatory = False,
        ),
        "package": attr.string(
            doc = "The fully qualified package name for the input sources.",
            mandatory = True,
        ),
        "out": attr.output(
            doc = "Name of the generator output file.",
            mandatory = True,
        ),
        "_tool": attr.label(
            executable = True,
            cfg = "exec",
            default = Label("//tools/go_stateify:stateify"),
        ),
        "_statepkg": attr.string(default = "gvisor.dev/gvisor/pkg/state"),
    },
)

def _go_stateify_records_impl(ctx):
    """Type-checks and renders a selected package in one stateify action."""
    go = go_context(ctx)
    archives = {}
    archive_files = []
    canonical_archives = {}
    for data in depset(transitive = [go_transitive_archives(dep) for dep in ctx.attr.deps]).to_list():
        export = data.export_file if data.export_file else data.file
        archive_files.append(export)
        if data.importmap in canonical_archives and canonical_archives[data.importmap] != export.path:
            fail("conflicting exports for compiler package %s" % data.importmap)
        canonical_archives[data.importmap] = export.path
        entry = {"File": export.path, "ImportMap": data.importmap}
        for path in [data.importpath, data.importmap] + list(data.importpath_aliases):
            if path in archives and archives[path] != entry:
                fail("conflicting exports for %s" % path)
            archives[path] = entry
    sources = {f.short_path: f.path for f in ctx.files.srcs}
    groups = []
    for name, output in zip(ctx.attr.group_names, ctx.outputs.outs):
        groups.append({
            "Sources": [sources[ctx.label.package + "/" + path] for path in ctx.attr.groups[name]],
            "Output": output.path,
        })
    tags = list(go.gotags) + ctx.attr.gotags + [arg[1:] for arg in go.nogo_args]
    config = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.write(config, json.encode({
        "Package": ctx.attr.package,
        "ImportPath": ctx.attr.importpath,
        "StatePackage": "gvisor.dev/gvisor/pkg/state",
        "Imports": ctx.attr.imports,
        "Sources": [f.path for f in ctx.files.srcs],
        "Groups": groups,
        "Archives": archives,
        "Stdlib": [f.path for f in go.stdlib_archives.to_list()],
        "GOOS": go.env["GOOS"],
        "GOARCH": go.env["GOARCH"],
        "Tags": tags,
        "GoVersion": go.lang_version,
    }))
    ctx.actions.run(
        inputs = depset([config] + ctx.files.srcs + archive_files, transitive = [go.stdlib_archives]),
        outputs = ctx.outputs.outs,
        mnemonic = "GoStateify",
        progress_message = "Generating typed state records %s" % ctx.label,
        arguments = ["-typed-config=" + config.path],
        executable = ctx.executable._tool,
        env = go.env,
    )

# The action uses the selected target's source tags, word size and compiler
# exports. The generator itself runs in the execution configuration.
go_stateify_records = go_rule(
    rule,
    implementation = _go_stateify_records_impl,
    attrs = {
        "srcs": attr.label_list(allow_files = True, mandatory = True),
        "deps": attr.label_list(),
        "gotags": attr.string_list(),
        "groups": attr.string_list_dict(mandatory = True),
        "group_names": attr.string_list(mandatory = True),
        "outs": attr.output_list(mandatory = True),
        "imports": attr.string_list(),
        "package": attr.string(mandatory = True),
        "importpath": attr.string(mandatory = True),
        "_tool": attr.label(
            executable = True,
            cfg = "exec",
            default = Label("//tools/go_stateify:stateify"),
        ),
    },
)
