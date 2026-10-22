"""Expose the execution C toolchain to BusyBox's upstream HOSTCC calls."""

load("@bazel_skylib//lib:shell.bzl", "shell")
load("@rules_cc//cc:action_names.bzl", "ACTION_NAMES")
load("@rules_cc//cc:find_cc_toolchain.bzl", "find_cc_toolchain", "use_cc_toolchain")
load("@rules_cc//cc/common:cc_common.bzl", "cc_common")

def _argument(ctx, value):
    # Make runs in a copied source directory; toolchain paths refer to execroot.
    value = value.replace("$EXT_BUILD_ROOT", "$$EXT_BUILD_ROOT$$")
    for prefix in ["external/", "bazel-out/", ctx.workspace_name + "/"]:
        before, separator, after = value.partition(prefix)
        if separator and not before.endswith("/"):
            value = before + "$$EXT_BUILD_ROOT$$/" + separator + after
            break

    # Quote literal arguments while expanding only the foreign_cc execution root.
    return '"${EXT_BUILD_ROOT}"'.join([
        shell.quote(part)
        for part in value.split("$$EXT_BUILD_ROOT$$")
    ])

def _busybox_host_compiler_impl(ctx):
    toolchain = find_cc_toolchain(ctx)

    # Upstream Make owns individual compilation actions and cannot supply the
    # per-action metadata needed by these Bazel instrumentation/module features.
    disabled = ctx.disabled_features + [
        "fdo_instrument",
        "fdo_optimize",
        "layering_check",
        "module_maps",
        "thin_lto",
    ]
    if not ctx.coverage_instrumented():
        disabled.append("coverage")
    features = cc_common.configure_features(
        ctx = ctx,
        cc_toolchain = toolchain,
        requested_features = ctx.features,
        unsupported_features = disabled,
    )
    compiler = _argument(ctx, cc_common.get_tool_for_action(
        feature_configuration = features,
        action_name = ACTION_NAMES.c_compile,
    ))
    compile_variables = cc_common.create_compile_variables(
        feature_configuration = features,
        cc_toolchain = toolchain,
        user_compile_flags = ctx.fragments.cpp.copts + ctx.fragments.cpp.conlyopts,
        use_pic = toolchain.needs_pic_for_dynamic_libraries(feature_configuration = features),
    )
    link_variables = cc_common.create_link_variables(
        feature_configuration = features,
        cc_toolchain = toolchain,
        user_link_flags = ctx.fragments.cpp.linkopts,
        is_using_linker = True,
    )
    flags = []
    environment = {}
    for action, variables in [
        (ACTION_NAMES.c_compile, compile_variables),
        (ACTION_NAMES.cpp_link_executable, link_variables),
    ]:
        flags.append(" ".join([
            _argument(ctx, flag)
            for flag in cc_common.get_memory_inefficient_command_line(
                feature_configuration = features,
                action_name = action,
                variables = variables,
            )
        ]))
        environment.update(cc_common.get_environment_variables(
            feature_configuration = features,
            action_name = action,
            variables = variables,
        ))
    lines = [
        "#!/bin/sh",
        "set -eu",
        ': "${EXT_BUILD_ROOT:?rules_foreign_cc execution root is required}"',
    ]

    # Combined HOSTCC calls need both environments; link settings take precedence.
    for name, value in environment.items():
        lines.append("export {}={}".format(name, _argument(ctx, value)))
    lines.extend([
        "for argument do",
        '  case "$argument" in -c|-E|-S)',
        '    exec {} {} "$@" ;;'.format(compiler, flags[0]),
        "  esac",
        "done",
        # HOSTCC compiles and links C sources in one call. Keep the C driver and
        # compilation flags when adding the selected toolchain's link flags.
        'exec {} {} {} "$@"'.format(compiler, flags[0], flags[1]),
    ])
    ctx.actions.write(ctx.outputs.executable, "\n".join(lines) + "\n", is_executable = True)
    return [DefaultInfo(
        executable = ctx.outputs.executable,
        runfiles = ctx.runfiles(transitive_files = toolchain.all_files),
    )]

# foreign_cc requests this target through build_data (cfg = "exec"). Its target
# CC and flags come from the independently resolved target C toolchain.
busybox_host_compiler = rule(
    implementation = _busybox_host_compiler_impl,
    executable = True,
    fragments = ["cpp"],
    toolchains = use_cc_toolchain(),
)

def _busybox_nm_impl(ctx):
    toolchain = find_cc_toolchain(ctx)
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
    toolchains = use_cc_toolchain(),
)
