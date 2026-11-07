"""Small local fixture for the exact release-input manifest and shell argv."""

def _data_impl(ctx):
    data = ctx.actions.declare_file(ctx.label.name + "/" + ctx.attr.filename)
    ctx.actions.write(data, "local boundary data\n")
    return [DefaultInfo(files = depset([data]))]

data_fixture = rule(implementation = _data_impl, attrs = {"filename": attr.string(mandatory = True)})

def _tree_impl(ctx):
    dist = ctx.actions.declare_directory(ctx.label.name + " tree, space")
    runtime = ctx.toolchains["@rules_python//python:toolchain_type"].py3_runtime
    args = ctx.actions.args()
    args.add_all(["-I", "-B", "-c", "import pathlib,sys; root=pathlib.Path(sys.argv[1]); root.mkdir(exist_ok=True); [(root / n).write_bytes(n.encode()) for n in ('gvisor-1.0-py3-none-any.whl','gvisor-1.0.tar.gz')]"])
    args.add(dist.path)
    ctx.actions.run(
        executable = runtime.interpreter,
        arguments = [args],
        inputs = runtime.files,
        outputs = [dist],
        toolchain = "@rules_python//python:toolchain_type",
        mnemonic = "ReleaseBoundaryTree",
    )
    return [DefaultInfo(files = depset([dist]))]

tree_fixture = rule(
    implementation = _tree_impl,
    toolchains = ["@rules_python//python:toolchain_type"],
)
