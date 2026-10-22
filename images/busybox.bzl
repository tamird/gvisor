"""Expose the execution C toolchain to BusyBox's upstream HOSTCC calls."""

load("@bazel_skylib//lib:shell.bzl", "shell")
load("@bazel_tools//tools/cpp:toolchain_utils.bzl", "find_cpp_toolchain")
load("@rules_foreign_cc//foreign_cc/private:cc_toolchain_util.bzl", "get_env_vars", "get_flags_info", "get_tools_info")
load("@rules_foreign_cc//foreign_cc/private:flag_utils.bzl", "absolutize_path_for_build_root")
load("@rules_foreign_cc//foreign_cc/private:framework.bzl", "FOREIGN_CC_FRAMEWORK_COMMON_ATTRS")
load("@rules_foreign_cc//foreign_cc/private/framework:platform.bzl", "PLATFORM_CONSTRAINTS_RULE_ATTRIBUTES")

def _argument(ctx, value):
    value = value.replace("$EXT_BUILD_ROOT", "$$EXT_BUILD_ROOT$$")
    value = absolutize_path_for_build_root(ctx.workspace_name, value)
    # Quote literal arguments while expanding only the foreign_cc execution root.
    return '"${EXT_BUILD_ROOT}"'.join([
        shell.quote(part)
        for part in value.split("$$EXT_BUILD_ROOT$$")
    ])

def _busybox_host_compiler_impl(ctx):
    tools = get_tools_info(ctx)
    flags = get_flags_info(ctx)
    compiler = _argument(ctx, tools.cc)
    compile_flags = " ".join([_argument(ctx, flag) for flag in flags.cc])
    link_flags = " ".join([_argument(ctx, flag) for flag in flags.cxx_linker_executable])
    lines = [
        "#!/bin/sh",
        "set -eu",
        ': "${EXT_BUILD_ROOT:?rules_foreign_cc execution root is required}"',
    ]
    # foreign_cc merges compilation and link environments in this same order.
    for name, value in get_env_vars(ctx).items():
        lines.append("export {}={}".format(name, _argument(ctx, value)))
    lines.extend([
        "for argument do",
        '  case "$argument" in -c|-E|-S)',
        '    exec {} {} "$@" ;;'.format(compiler, compile_flags),
        "  esac",
        "done",
        # HOSTCC compiles and links C sources in one call. Use the C driver with
        # the same toolchain flag exports that foreign_cc passes to upstream Make.
        'exec {} {} {} "$@"'.format(compiler, compile_flags, link_flags),
    ])
    ctx.actions.write(ctx.outputs.executable, "\n".join(lines) + "\n", is_executable = True)
    return [DefaultInfo(
        executable = ctx.outputs.executable,
        runfiles = ctx.runfiles(transitive_files = find_cpp_toolchain(ctx).all_files),
    )]

# foreign_cc requests this target through build_data (cfg = "exec"). Its target
# CC and flags come from the independently resolved target C toolchain.
busybox_host_compiler = rule(
    implementation = _busybox_host_compiler_impl,
    attrs = {
        name: FOREIGN_CC_FRAMEWORK_COMMON_ATTRS[name]
        for name in ["copts", "linkopts", "set_file_prefix_map", "_set_file_prefix_map_default"]
    } | PLATFORM_CONSTRAINTS_RULE_ATTRIBUTES,
    executable = True,
    fragments = ["cpp"],
    toolchains = ["@bazel_tools//tools/cpp:toolchain_type"],
)

def _busybox_nm_impl(ctx):
    toolchain = find_cpp_toolchain(ctx)
    ctx.actions.write(
        ctx.outputs.executable,
        '#!/bin/sh\nset -eu\n: "${{EXT_BUILD_ROOT:?}}"\nexec {} "$@"\n'.format(_argument(ctx, toolchain.nm_executable)),
        is_executable = True,
    )
    return [DefaultInfo(
        executable = ctx.outputs.executable,
        runfiles = ctx.runfiles(transitive_files = toolchain.all_files),
    )]

busybox_nm = rule(
    implementation = _busybox_nm_impl,
    executable = True,
    toolchains = ["@bazel_tools//tools/cpp:toolchain_type"],
)
