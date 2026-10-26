"""Build and run the pinned upstream gVisor smoke test with declared tools."""

load("@bazel_skylib//lib:shell.bzl", "shell")
load("@bazel_skylib//rules/directory:providers.bzl", "DirectoryInfo")
load("@io_bazel_rules_go//go:def.bzl", "go_context")
load("@rules_foreign_cc//toolchains/native_tools:tool_access.bzl", "access_tool")
load("@tar.bzl//tar:tar.bzl", "tar_lib")
load("//tools/bazeldefs:go.bzl", "go_rule")

_SyzkallerInfo = provider(
    "Pinned binaries and the compiler closure used by upstream runtime probes.",
    fields = ["tree", "config", "compiler_files"],
)

def _build_impl(ctx):
    go = go_context(ctx, maybe_needs_cc_toolchain = False)
    make = access_tool(Label("@rules_foreign_cc//toolchains:make_toolchain"), ctx)
    if make.target == None or make.env != {"MAKE": make.path}:
        fail("Syzkaller requires the declared GNU Make toolchain and its MAKE environment")

    tar = ctx.toolchains[tar_lib.toolchain_type]
    resource_dir = ctx.attr._resource_dir[DirectoryInfo]
    compiler_files = depset([ctx.file._sysroot], transitive = [
        ctx.attr._clang[DefaultInfo].files,
        ctx.attr._clangxx[DefaultInfo].files,
        ctx.attr._linker[DefaultInfo].files,
        resource_dir.transitive_files,
        tar.default.files,
    ])
    roots = {}
    for file in compiler_files.to_list():
        root = file.path.split("/")[0]
        if root not in ("external", "bazel-out"):
            fail("Unexpected compiler input outside external/ or bazel-out/: " + file.path)
        roots[root] = True
    config = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.write(config, json.encode({
        "sources": [file.path for file in ctx.files._sources],
        "source_root": ctx.file._go_mod.dirname,
        "go": go.sdk.go.path,
        "goroot": go.sdk.root_file.dirname,
        "make": make.path,
        "proxy": ctx.file._proxy_root.dirname,
        "cc": ctx.executable._clang.path,
        "cxx": ctx.executable._clangxx.path,
        "linker": ctx.executable._linker.path,
        "resource_dir": resource_dir.path,
        "sysroot": ctx.file._sysroot.path,
        "tar": tar.tarinfo.binary.path,
        "tar_env": [key + "=" + tar.tarinfo.default_env[key] for key in sorted(tar.tarinfo.default_env)],
        "tool_roots": sorted(roots),
    }))
    tree = ctx.actions.declare_directory(ctx.label.name)
    ctx.actions.run(
        executable = ctx.executable._stage,
        arguments = ["-config", config.path, "-output", tree.path],
        inputs = depset([config, ctx.file._proxy_root], transitive = [
            ctx.attr._sources[DefaultInfo].files,
            ctx.attr._proxy[DefaultInfo].files,
            depset([go.sdk.go]),
            go.sdk.srcs,
            go.sdk.headers,
            go.sdk.tools,
            compiler_files,
            make.target.files,
        ]),
        tools = [ctx.attr._stage[DefaultInfo].files_to_run],
        outputs = [tree],
        mnemonic = "SyzkallerBuild",
        progress_message = "Building pinned Syzkaller manager and executors",
    )
    return [
        DefaultInfo(files = depset([tree])),
        _SyzkallerInfo(tree = tree, config = config, compiler_files = compiler_files),
    ]

syzkaller_build = go_rule(
    rule,
    implementation = _build_impl,
    attrs = {
        "_go_mod": attr.label(default = "@syzkaller//:go.mod", allow_single_file = True),
        "_sources": attr.label(default = "@syzkaller//:sources"),
        "_proxy": attr.label(default = "@syzkaller_modules//:files"),
        "_proxy_root": attr.label(default = "@syzkaller_modules//:modules/cache/download/ROOT", allow_single_file = True),
        "_stage": attr.label(default = "//test/syzkaller:stage", executable = True, cfg = "exec"),
        "_clang": attr.label(default = "@llvm//tools:clang", executable = True, allow_single_file = True, cfg = "exec"),
        "_clangxx": attr.label(default = "@llvm//tools:clang++", executable = True, allow_single_file = True, cfg = "exec"),
        "_linker": attr.label(default = "@llvm//tools:ld.lld", executable = True, allow_single_file = True, cfg = "exec"),
        "_resource_dir": attr.label(default = "@llvm//:builtin_resource_dir", providers = [DirectoryInfo], cfg = "exec"),
        "_sysroot": attr.label(default = "@syzkaller_sysroot//:flat", allow_single_file = True, cfg = "exec"),
    },
    # Bind the toolchains here before calling rules_go's factory.
    toolchains = [
        Label("@rules_foreign_cc//toolchains:make_toolchain"),
        Label(tar_lib.toolchain_type),
    ],
)

def _smoke_impl(ctx):
    syz = ctx.attr.syzkaller[_SyzkallerInfo]
    launcher = ctx.actions.declare_file(ctx.label.name)
    tool_root = ctx.label.name + "_compiler"
    ctx.actions.write(
        launcher,
        "#!/bin/bash\nset -euo pipefail\nexec %s -config %s -source %s -runtime %s -tools \"$TEST_SRCDIR/%s\"\n" % (
            shell.quote(ctx.executable._stage.short_path),
            shell.quote(syz.config.short_path),
            shell.quote(syz.tree.short_path),
            shell.quote(ctx.file.runtime.short_path),
            tool_root,
        ),
        is_executable = True,
    )
    runfiles = ctx.runfiles(
        files = [syz.tree, syz.config, ctx.file.runtime, ctx.executable._stage],
        root_symlinks = {tool_root + "/" + file.path: file for file in syz.compiler_files.to_list()},
    ).merge(ctx.attr._stage[DefaultInfo].default_runfiles)
    return DefaultInfo(executable = launcher, runfiles = runfiles)

syzkaller_smoke_test = rule(
    implementation = _smoke_impl,
    test = True,
    attrs = {
        "runtime": attr.label(mandatory = True, allow_single_file = True),
        "syzkaller": attr.label(mandatory = True, providers = [_SyzkallerInfo]),
        "_stage": attr.label(default = "//test/syzkaller:stage", executable = True, cfg = "target"),
    },
)
