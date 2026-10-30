load(":runtime_vars.bzl", "RUNTIME")

def _path(root, path):
    return root + path.lstrip("/")

def _probe_impl(ctx):
    out = ctx.actions.declare_file(ctx.label.name + ".json")
    root = ctx.label.package + "/tool_files/"
    lib = ":".join([_path(root, d) for d in RUNTIME["library_path"]])
    args = ctx.actions.args()
    args.add_all(["--library-path", lib, _path(root, RUNTIME["ruby"]), ctx.file.driver.path,
                  out.path, ctx.label.package + "/gems", _path(root, RUNTIME["loader"]),
                  lib, _path(root, RUNTIME["node"]), ctx.file.checks.path])
    ctx.actions.run(
        executable = ctx.executable.loader,
        arguments = [args],
        inputs = depset(ctx.files.runtime + ctx.files.gems + [ctx.file.driver, ctx.file.checks]),
        outputs = [out],
        env = {
            "RUBYLIB": ":".join([_path(root, d) for d in RUNTIME["load_paths"]]),
            "GEM_PATH": ":".join([_path(root, d) for d in RUNTIME["gem_paths"]]),
            "PATH": "",
            "LANG": "C.UTF-8",
        },
        mnemonic = "DeclaredJekyllRuntime",
        toolchain = None,
    )
    return [DefaultInfo(files = depset([out]))]

probe = rule(
    implementation = _probe_impl,
    attrs = {
        "runtime": attr.label_list(allow_files = True),
        "driver": attr.label(allow_single_file = True),
        "checks": attr.label(allow_single_file = True),
        "gems": attr.label_list(allow_files = True),
        "loader": attr.label(executable = True, cfg = "exec", allow_files = True),
    },
)
