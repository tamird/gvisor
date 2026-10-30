"""Render the gVisor website and generate the syscall reference from declared inputs."""

load("@bazel_skylib//rules:common_settings.bzl", "BuildSettingInfo")
load("@rules_img//img:providers.bzl", "ImageManifestInfo")
load("//website:defs.bzl", "SiteDocs")

# The system and gem directories of the official Alpine Jekyll runtime.
_RUBY_LIBRARIES = [
    "site_ruby/2.7.0",
    "site_ruby/2.7.0/x86_64-linux-musl",
    "site_ruby",
    "vendor_ruby/2.7.0",
    "vendor_ruby/2.7.0/x86_64-linux-musl",
    "vendor_ruby",
    "2.7.0",
    "2.7.0/x86_64-linux-musl",
]

def _syscalls_impl(ctx):
    syscall_json = ctx.actions.declare_file("syscalls.json")
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
    root = ctx.file.runtime.path + "/"
    def_args = ctx.actions.declare_file(ctx.label.name + ".inputs.json")
    docs = [t[SiteDocs] for t in ctx.attr.docs]
    as_of = ctx.attr.as_of[BuildSettingInfo].value
    ctx.actions.write(def_args, json.encode({
        "as_of": as_of,
        "workspace_status": "" if as_of else ctx.version_file.path,
        "site": [(f.path, f.short_path.removeprefix("website/")) for f in ctx.files.srcs],
        "static": [(f.path, f.short_path.removeprefix("website/")) for f in ctx.files.static],
        "docs": [doc for group in docs for doc in group.entries],
        "syscalls": ctx.file.syscalls.path,
        "css": ctx.file.css.path,
        "gems": [f.path for f in ctx.files.gems],
    }))
    loader = root + "lib/ld-musl-x86_64.so.1"
    lib = ":".join([root + d for d in ["lib", "usr/local/lib", "usr/lib"]])
    args = ctx.actions.args()
    args.add_all([
        "--library-path",
        lib,
        root + "usr/local/bin/ruby",
        # The bootstrap resolves SDK paths before the rendering Ruby loads its gems.
        "--disable-gems",
        ctx.file.driver.path,
        def_args.path,
        output.path,
        loader,
        lib,
        root + "usr/bin/node",
        ctx.file.checks.path,
    ])
    ctx.actions.run(
        executable = loader,
        arguments = [args],
        inputs = depset(
            ([] if as_of else [ctx.version_file]) + ctx.files.srcs + ctx.files.static + [ctx.file.runtime] + ctx.files.gems +
            [def_args, ctx.file.syscalls, ctx.file.css, ctx.file.driver, ctx.file.checks],
            transitive = [target.files for target in ctx.attr.docs],
        ),
        outputs = [output],
        env = {
            "LANG": "C.UTF-8",
            "GEM_PATH": ":".join([root + "usr/local/lib/ruby/gems/2.7.0", root + "usr/gem"]),
            "PATH": "",
            "RUBYLIB": ":".join([root + "usr/local/lib/ruby/" + d for d in _RUBY_LIBRARIES]),
        },
        mnemonic = "JekyllSite",
        toolchain = None,
    )
    return [DefaultInfo(files = depset([output]))]

render_site = rule(
    implementation = _site_impl,
    attrs = {
        "as_of": attr.label(mandatory = True, providers = [BuildSettingInfo]),
        "srcs": attr.label_list(allow_files = True),
        "static": attr.label_list(allow_files = True),
        "docs": attr.label_list(providers = [SiteDocs]),
        "syscalls": attr.label(allow_single_file = True),
        "css": attr.label(allow_single_file = True),
        "driver": attr.label(allow_single_file = True),
        "checks": attr.label(allow_single_file = True),
        "runtime": attr.label(allow_single_file = True, cfg = "exec"),
        "gems": attr.label_list(allow_files = True),
    },
)

_TAR = "@tar.bzl//tar/toolchain:type"

def _sdk_impl(ctx):
    output = ctx.actions.declare_directory(ctx.label.name)

    # image_flatten supplies a single tar after applying all OCI whiteouts.
    blob = ctx.attr.image[ImageManifestInfo].layers[0].blob
    tar = ctx.toolchains[_TAR]
    args = ctx.actions.args()
    args.add_all(["-xf", blob.path, "-C", output.path, "--no-same-owner", "--no-xattrs", "--no-acls", "--no-fflags"])
    for pattern in ["usr/local/bin/ruby", "usr/bin/node", "lib/*.so*", "usr/lib/*.so*", "usr/local/lib/*.so*", "usr/local/lib/ruby", "usr/gem", "usr/share/zoneinfo/America/Chicago"]:
        args.add("--include", pattern)
    for pattern in ["usr/gem/cache", "usr/gem/doc", "usr/local/lib/ruby/gems/2.7.0/cache", "usr/local/lib/ruby/gems/2.7.0/doc", ".git"]:
        args.add("--exclude", pattern)
    ctx.actions.run(
        executable = tar.tarinfo.binary,
        inputs = depset([blob], transitive = [tar.default.files]),
        outputs = [output],
        arguments = [args],
        mnemonic = "JekyllSDK",
        toolchain = _TAR,
    )
    return [DefaultInfo(files = depset([output]))]

jekyll_sdk = rule(
    implementation = _sdk_impl,
    attrs = {"image": attr.label(mandatory = True, providers = [ImageManifestInfo])},
    toolchains = [_TAR],
)
