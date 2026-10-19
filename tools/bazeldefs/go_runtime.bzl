"""Runtime tests consuming one separately declared Go test compilation."""

load("@io_bazel_rules_go//go:def.bzl", "GoArchive")

def _go_runtime_test_impl(ctx):
    compiled = ctx.attr.src
    original = ctx.executable.src

    # Preserve the compiled executable's relative output directory depth.
    # Encode the full owner name without slash aliases.
    owner = ctx.label.name.replace("_", "__").replace("/", "_s")
    executable = ctx.actions.declare_file("runtime_" + owner + ".exe", sibling = original)
    ctx.actions.symlink(output = executable, target_file = original, is_executable = True)

    # Match rules_go's data_runfiles contract; native_test instead merges the
    # data targets' default_runfiles, which need not contain the same files.
    runtime_runfiles = ctx.runfiles(
        files = [executable, original],
        transitive_files = depset(transitive = [dep[DefaultInfo].files for dep in ctx.attr.data]),
    ).merge_all([dep[DefaultInfo].data_runfiles for dep in ctx.attr.data])
    defaults = compiled[DefaultInfo]
    environment = dict(compiled[RunEnvironmentInfo].environment)
    for key, value in ctx.attr.env.items():
        environment[key] = ctx.expand_location(value, ctx.attr.data) if "$" in value else value

    return [
        compiled[GoArchive],
        compiled[OutputGroupInfo],
        DefaultInfo(
            executable = executable,
            files = depset([executable]),
            default_runfiles = defaults.default_runfiles.merge(runtime_runfiles),
            data_runfiles = defaults.data_runfiles.merge(runtime_runfiles),
        ),
        RunEnvironmentInfo(
            environment = environment,
            inherited_environment = compiled[RunEnvironmentInfo].inherited_environment + ctx.attr.env_inherit,
        ),
        coverage_common.instrumented_files_info(ctx, dependency_attributes = ["src", "data"]),
    ]

go_runtime_test = rule(
    implementation = _go_runtime_test_impl,
    test = True,
    attrs = {
        "src": attr.label(executable = True, cfg = "target", mandatory = True, providers = [GoArchive]),
        "data": attr.label_list(allow_files = True),
        "env": attr.string_dict(),
        "env_inherit": attr.string_list(),
        "_lcov_merger": attr.label(
            default = configuration_field(fragment = "coverage", name = "output_generator"),
            executable = True,
            cfg = config.exec("test"),
        ),
        "_collect_cc_coverage": attr.label(
            default = "@bazel_tools//tools/test:collect_cc_coverage",
            executable = True,
            cfg = config.exec("test"),
        ),
    },
)
