"""Use the native release configuration already checked on each worker."""

def _set_options(_settings, attr):
    worker = "//test/rbe/release_prewarmer:" + attr.arch
    return {
        "//command_line_option:cpu": "k8" if attr.arch == "amd64" else "aarch64",
        "//command_line_option:extra_execution_platforms": [worker],
        "//command_line_option:extra_toolchains": ["//test/rbe/go_bootstrap:linux_" + attr.arch + "_ash"],
        "//command_line_option:host_platform": worker,
        "//command_line_option:platforms": "//tools/bazeldefs:linux_amd64" if attr.arch == "amd64" else "@llvm//platforms:linux_aarch64_glibc_static",
    }

_native = transition(
    implementation = _set_options,
    inputs = [],
    outputs = [
        "//command_line_option:cpu",
        "//command_line_option:extra_execution_platforms",
        "//command_line_option:extra_toolchains",
        "//command_line_option:host_platform",
        "//command_line_option:platforms",
    ],
)

def _release_impl(ctx):
    # Give the two transitioned targets independent top-level handles. Each is
    # a symlink to the original public producer and no bytes are repackaged.
    outputs = []
    for suffix, inputs in [
        ("tar.bz2", ctx.files.archive),
        ("runsc", ctx.files.runsc),
        ("sentry", ctx.files.sentry),
    ]:
        [src] = inputs
        out = ctx.actions.declare_file(ctx.label.name + "." + suffix)
        ctx.actions.symlink(output = out, target_file = src)
        outputs.append(out)
    return [DefaultInfo(files = depset(outputs))]

native_release = rule(
    implementation = _release_impl,
    attrs = {
        "arch": attr.string(values = ["amd64", "arm64"]),
        "archive": attr.label(cfg = _native, allow_single_file = True),
        "runsc": attr.label(cfg = _native, allow_single_file = True),
        "sentry": attr.label(cfg = _native, allow_single_file = True),
        "_allowlist_function_transition": attr.label(
            default = "@bazel_tools//tools/allowlists/function_transition_allowlist",
        ),
    },
)
