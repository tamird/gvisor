"""Render the gVisor website and generate the syscall reference from declared inputs."""

load("//test/rbe/website-jekyll:runtime_vars.bzl", "RUNTIME")
load("//website:defs.bzl", "SiteDocs")

def _syscalls_impl(ctx):
    syscall_json = ctx.actions.declare_file("reference.json")
    pages = ctx.actions.declare_directory(ctx.label.name)
    ctx.actions.run(
        executable = ctx.executable._sentry,
        arguments = ["--", "help", "syscalls", "-format", "json", "-filename", syscall_json.path],
        outputs = [syscall_json],
        mnemonic = "WebsiteSyscallJSON",
        toolchain = None,
    )
    ctx.actions.run(
        executable = ctx.executable._generator,
        arguments = ["-in", syscall_json.path, "-out", pages.path],
        inputs = [syscall_json],
        outputs = [pages],
        mnemonic = "WebsiteSyscallDocs",
        toolchain = None,
    )
    return [DefaultInfo(files = depset([pages]))]

syscall_pages = rule(
    implementation = _syscalls_impl,
    attrs = {
        "_sentry": attr.label(default = "//runsc/cmd/sentry:gvisor_sentry", executable = True, cfg = "exec"),
        "_generator": attr.label(default = "//website/cmd/syscalldocs", executable = True, cfg = "exec"),
    },
)

def _site_impl(ctx):
    output = ctx.actions.declare_directory(ctx.label.name)
    root = "test/rbe/website-jekyll/tool_files/"
    def_args = ctx.actions.declare_file(ctx.label.name + ".inputs.json")
    docs = [t[SiteDocs] for t in ctx.attr.docs]
    ctx.actions.write(def_args, json.encode({
        "as_of": ctx.attr.as_of,
        "site": [(f.path, f.short_path.removeprefix("website/")) for f in ctx.files.srcs],
        "static": [(f.path, f.short_path.removeprefix("website/")) for f in ctx.files.static],
        "docs": [doc for group in docs for doc in group.entries],
        "syscalls": ctx.file.syscalls.path,
        "legacy_css_tar": ctx.file.css.path,
        "gems": [f.path for f in ctx.files.gems],
    }))
    lib = ":".join([root + d.lstrip("/") for d in RUNTIME["library_path"]])
    args = ctx.actions.args()
    args.add_all(["--library-path", lib, root + RUNTIME["ruby"].lstrip("/"),
                  ctx.file.driver.path, def_args.path, output.path, root + RUNTIME["loader"],
                  lib, root + RUNTIME["node"].lstrip("/"), ctx.file.checks.path])
    ctx.actions.run(
        executable = ctx.executable.loader,
        arguments = [args],
        inputs = depset(ctx.files.srcs + ctx.files.static + ctx.files.runtime + ctx.files.gems +
                        [def_args, ctx.file.syscalls, ctx.file.css, ctx.file.driver, ctx.file.checks],
                        transitive = [group.files for group in docs]),
        outputs = [output],
        env = {
            "LANG": "C.UTF-8",
            "GEM_PATH": ":".join([root + d.lstrip("/") for d in RUNTIME["gem_paths"]]),
            "PATH": "",
            "RUBYLIB": ":".join([root + d.lstrip("/") for d in RUNTIME["load_paths"]]),
        },
        mnemonic = "JekyllSite",
        toolchain = None,
    )
    return [DefaultInfo(files = depset([output]))]

render_site = rule(
    implementation = _site_impl,
    attrs = {
        "as_of": attr.string(mandatory = True),
        "srcs": attr.label_list(allow_files = True),
        "static": attr.label_list(allow_files = True),
        "docs": attr.label_list(providers = [SiteDocs]),
        "syscalls": attr.label(allow_single_file = True),
        "css": attr.label(allow_single_file = True),
        "driver": attr.label(allow_single_file = True),
        "checks": attr.label(allow_single_file = True),
        "runtime": attr.label_list(allow_files = True),
        "gems": attr.label_list(allow_files = True),
        "loader": attr.label(executable = True, cfg = "exec", allow_files = True),
    },
)
