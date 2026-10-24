"""Build the exported module with the Go command and declared toolchains."""

load("@io_bazel_rules_go//go:def.bzl", "go_context")
load("@rules_cc//cc:action_names.bzl", "ACTION_NAMES")
load("@rules_cc//cc/common:cc_common.bzl", "cc_common")
load("@with_cfg.bzl//:with_cfg.bzl", "with_cfg")
load("//tools/bazeldefs:go.bzl", "go_rule")

def _module_build_impl(ctx):
    go = go_context(ctx)
    output = ctx.actions.declare_file(ctx.label.name + ".json")
    args = ctx.actions.args()
    args.add("--archive", ctx.file.archive)
    args.add("--go", go.sdk.go)
    args.add("--goroot", go.sdk.root_file.dirname)
    args.add("--proxy", ctx.file._proxy_root.dirname)
    args.add("--output", output)
    args.add("--goos", ctx.attr.goos)
    args.add("--goarch", ctx.attr.goarch)
    inputs = [ctx.attr._proxy[DefaultInfo].files, go.sdk.srcs, go.sdk.headers, go.sdk.tools]
    if ctx.attr.cgo:
        if go.cgo_tools == None:
            fail("The exported Linux module build requires a C toolchain")
        cc = go.cgo_tools
        args.add("--cc", cc.c_compiler_path)
        args.add("--cxx", cc_common.get_tool_for_action(
            feature_configuration = cc.feature_configuration,
            action_name = ACTION_NAMES.cpp_compile,
        ))
        args.add_all(cc.c_compile_options, format_each = "--cflag=%s")
        args.add_all(cc.cxx_compile_options, format_each = "--cxxflag=%s")
        args.add_all(cc.ld_executable_options, format_each = "--ldflag=%s")
        inputs.append(go.cc_toolchain_files)
    args.add_all(ctx.attr.packages)
    ctx.actions.run(
        executable = ctx.executable._builder,
        arguments = [args],
        inputs = depset([ctx.file.archive, ctx.file._proxy_root, go.sdk.go], transitive = inputs),
        tools = [ctx.attr._builder[DefaultInfo].files_to_run],
        outputs = [output],
        env = go.env,
        mnemonic = "ExportedGoBuild",
        progress_message = "Building exported Go module for %s/%s" % (ctx.attr.goos, ctx.attr.goarch),
    )
    return DefaultInfo(files = depset([output]))

module_build = go_rule(
    rule,
    implementation = _module_build_impl,
    attrs = {
        "archive": attr.label(default = "//:go_export", allow_single_file = True),
        "cgo": attr.bool(),
        "goarch": attr.string(mandatory = True),
        "goos": attr.string(mandatory = True),
        "packages": attr.string_list(mandatory = True),
        "_builder": attr.label(default = "//tools/go_export:compile", executable = True, cfg = "exec"),
        "_proxy": attr.label(default = "@exported_go_modules//:files"),
        "_proxy_root": attr.label(default = "@exported_go_modules//:modules/cache/download/ROOT", allow_single_file = True),
    },
)

# The exported Linux build includes cgo even when gVisor is built in pure mode.
# Keep the source archive in the caller's configuration: changing its Go mode
# would regenerate sources instead of checking the same exported module.
cgo_module_build, _cgo_module_build_reset = with_cfg(module_build).set(
    Label("@io_bazel_rules_go//go/config:pure"),
    False,
).resettable(Label(":cgo_original_settings")).reset_on_attrs("archive").build()
