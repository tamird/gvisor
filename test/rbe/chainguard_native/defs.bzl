"""Request the existing release binary on a matching native execution platform."""

def _native_options(_settings, attr):
    platform = "//tools/bazeldefs:linux_" + attr.arch
    return {
        "//command_line_option:cpu": "k8" if attr.arch == "amd64" else "aarch64",
        "//command_line_option:extra_execution_platforms": [platform],
        "//command_line_option:host_platform": platform,
        "//command_line_option:platforms": platform,
    }

_native = transition(
    implementation = _native_options,
    inputs = [],
    outputs = [
        "//command_line_option:cpu",
        "//command_line_option:extra_execution_platforms",
        "//command_line_option:host_platform",
        "//command_line_option:platforms",
    ],
)

def _result_impl(ctx):
    return [DefaultInfo(files = depset(ctx.files.observation))]

native_result = rule(
    implementation = _result_impl,
    attrs = {
        "arch": attr.string(values = ["amd64", "arm64"]),
        "observation": attr.label(cfg = _native),
        "_allowlist_function_transition": attr.label(
            default = "@bazel_tools//tools/allowlists/function_transition_allowlist",
        ),
    },
)

def _observe_impl(ctx):
    out = ctx.actions.declare_file(ctx.label.name + ".uname")
    sidecar = ctx.file.sidecar
    ctx.actions.run(
        executable = ctx.file.identity,
        inputs = [sidecar],
        outputs = [out],
        arguments = [out.path, sidecar.path],
        mnemonic = "ObserveNativeKernel",
        toolchain = None,
    )
    return [DefaultInfo(files = depset([out, sidecar]))]

observation = rule(
    implementation = _observe_impl,
    attrs = {
        "identity": attr.label(allow_single_file = True, mandatory = True),
        "sidecar": attr.label(allow_single_file = True, mandatory = True),
    },
)
