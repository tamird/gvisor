"""Architecture variants of maintained test declarations."""

load("@bazel_skylib//lib:shell.bzl", "shell")
load("@bazel_skylib//rules:common_settings.bzl", "BuildSettingInfo")
load("@with_cfg.bzl//:with_cfg.bzl", "FrontendInfo", "frontend_test", "with_cfg")

_ARCHITECTURES = {
    "amd64": struct(
        cpu = "k8",
        constraint = Label("@platforms//cpu:x86_64"),
        platform = Label("@io_bazel_rules_go//go/toolchain:linux_amd64_cgo"),
        static_platform = Label("@llvm//platforms:linux_x86_64_musl"),
    ),
    "arm64": struct(
        cpu = "aarch64",
        constraint = Label("@platforms//cpu:aarch64"),
        platform = Label("@io_bazel_rules_go//go/toolchain:linux_arm64_cgo"),
        static_platform = Label("@llvm//platforms:linux_aarch64_musl"),
    ),
}

def _native_frontend_impl(ctx):
    providers = ctx.super()
    local_architecture = ctx.attr._local_test_architecture[BuildSettingInfo].value
    constraints = [target.label for target in ctx.attr.exec_compatible_with]
    if not local_architecture or _ARCHITECTURES[local_architecture].constraint not in constraints:
        return providers
    if ctx.attr.exec_properties.get("test.workload-isolation-type") != "firecracker":
        return providers

    user = ctx.attr.exec_properties.get("test.dockerUser")
    if user not in ["root", "nobody"]:
        fail("unsupported local namespace test identity: %s" % user)
    if "no-local" in ctx.attr.tags:
        fail("local namespace test is tagged no-local")
    original_execution = ctx.attr.exports[testing.ExecutionInfo] if testing.ExecutionInfo in ctx.attr.exports else None
    requirements = dict(original_execution.requirements) if original_execution else {}
    if "no-local" in requirements:
        fail("local namespace test requires no-local")
    requirements["no-remote-exec"] = ""

    result = []
    for provider in providers:
        if provider == original_execution:
            continue
        if user == "root" and type(provider) == "DefaultInfo":
            # Keep Bazel's run_under outside this executable. Only the existing
            # root fixture and this test run as root, never the Bazel server.
            original_executable = ctx.attr.exports[FrontendInfo].executable
            executable = ctx.actions.declare_file(ctx.label.name + ".local_root")
            ctx.actions.write(
                executable,
                "#!/bin/bash\nexec \"${TEST_SRCDIR}/${TEST_WORKSPACE}\"%s \"${TEST_SRCDIR}/${TEST_WORKSPACE}\"%s \"$@\"\n" % (
                    shell.quote("/" + ctx.executable._local_root.short_path),
                    shell.quote("/" + original_executable.short_path),
                ),
                is_executable = True,
            )
            helper_runfiles = ctx.runfiles(files = [executable, original_executable, ctx.executable._local_root]).merge(
                ctx.attr._local_root[DefaultInfo].default_runfiles,
            )
            provider = DefaultInfo(
                executable = executable,
                files = provider.files,
                default_runfiles = provider.default_runfiles.merge(helper_runfiles),
                data_runfiles = provider.data_runfiles.merge(helper_runfiles),
            )
        result.append(provider)
    return result + [testing.ExecutionInfo(
        requirements = requirements,
        exec_group = original_execution.exec_group if original_execution else "test",
    )]

_native_frontend_test = rule(
    implementation = _native_frontend_impl,
    parent = frontend_test,
    attrs = {
        "_local_root": attr.label(default = Label("//test/rbe:local_root"), executable = True, cfg = "target"),
        "_local_test_architecture": attr.label(default = Label("//tools/bazeldefs:local_test_architecture")),
    },
)

def with_test_architecture(test_rule, architecture, static = False, extra_providers = [], implicit_targets = None):
    """Returns a with_cfg builder that preserves the test's other configuration."""
    target = _ARCHITECTURES[architecture]
    return with_cfg(
        test_rule,
        test_frontend = _native_frontend_test,
        extra_providers = extra_providers if testing.ExecutionInfo in extra_providers else extra_providers + [testing.ExecutionInfo],
        implicit_targets = implicit_targets,
    ).set("cpu", target.cpu).set(
        "platforms",
        [target.static_platform if static else target.platform],
    )

def test_architecture_tags(architectures, tags):
    """Exposes declared variants to the canonical qualification selector."""
    return tags + ["rbe-has-%s-variant" % architecture for architecture in architectures]

def test_architecture_variants(name, architectures, test_rules, kwargs):
    """Adds explicit, manual variants from the original test's complete attributes.

    Args:
        name: Original test name; variants append an architecture suffix.
        architectures: Target architectures to instantiate.
        test_rules: Architecture to configured test rule mapping.
        kwargs: Complete attributes of the original test declaration.
    """
    if len(architectures) != len(depset(architectures).to_list()):
        fail("duplicate test architectures: %s" % architectures)
    for architecture in architectures:
        if architecture not in _ARCHITECTURES:
            fail("unsupported test architecture: %s" % architecture)
        attributes = dict(kwargs)

        # Bazel 8.5's use_target_platform_for_tests ignores target exec_properties.
        # https://github.com/bazelbuild/bazel/blob/d84820503/src/main/java/com/google/devtools/build/lib/analysis/RuleContext.java#L428-L451
        # Matching execution constraints retain test.* worker requirements while
        # selecting native workers independently for each configured test.
        constraints = attributes.get("exec_compatible_with", []) + [
            Label("@platforms//os:linux"),
            _ARCHITECTURES[architecture].constraint,
        ]

        # Strings and Labels can name the same constraint. Resolve caller
        # strings in their BUILD package before removing duplicate values.
        attributes["exec_compatible_with"] = {
            native.package_relative_label(constraint): None
            for constraint in constraints
        }.keys()
        attributes["tags"] = attributes.get("tags", []) + ["manual"]
        test_rules[architecture](name = name + "_" + architecture, **attributes)
