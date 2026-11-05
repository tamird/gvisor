"""Expose the same Bazel-owned executable a test receives in its test exec group."""

def _test_wrapper_impl(ctx):
    return [DefaultInfo(files = ctx.attr.wrapper[DefaultInfo].files)]

test_wrapper_files = rule(
    implementation = _test_wrapper_impl,
    attrs = {
        "wrapper": attr.label(
            default = "@bazel_tools//tools/test:native_posix_test_wrapper",
            cfg = config.exec(exec_group = "test"),
        ),
    },
    exec_groups = {
        "test": exec_group(toolchains = [
            config_common.toolchain_type("@bazel_tools//tools/test:default_test_toolchain_type", mandatory = False),
        ]),
    },
)
