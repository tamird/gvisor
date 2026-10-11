"""Architecture variants of maintained test declarations."""

load("@bazel_skylib//lib:shell.bzl", "shell")
load("@bazel_skylib//rules:common_settings.bzl", "BuildSettingInfo")
load("@bazel_skylib//rules:native_binary.bzl", _native_test = "native_test")
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

_HOST_REQUIREMENT_PREFIX = "rbe-requires-host:"

def host_test_requirement_tags(requirements):
    """Declares host requirements independently of a worker routing policy."""
    return [_HOST_REQUIREMENT_PREFIX + requirement for requirement in requirements]

def _route_test(ctx, providers, original_executable):
    local_architecture = ctx.attr._local_test_architecture[BuildSettingInfo].value
    constraints = [target.label for target in ctx.attr.exec_compatible_with]
    if not local_architecture or _ARCHITECTURES[local_architecture].constraint not in constraints:
        return providers

    # The syscall macro declares KVM through its public platform tag. Its
    # remote properties deliberately make no claim of a KVM-capable worker.
    local_kvm = local_architecture == "amd64" and "runsc_kvm" in ctx.attr.tags
    if not local_kvm and ctx.attr.exec_properties.get("test.workload-isolation-type") != "firecracker":
        return providers

    initial_cgroup = "native" in ctx.attr.tags and "requires-initial-cgroup-namespace" in ctx.attr.tags
    if not local_kvm and not initial_cgroup:
        missing = ctx.attr._local_test_requirements[BuildSettingInfo].value
        required = ["namespace"] + [
            tag[len(_HOST_REQUIREMENT_PREFIX):]
            for tag in ctx.attr.tags
            if tag.startswith(_HOST_REQUIREMENT_PREFIX)
        ]
        if not any([requirement in missing for requirement in required]):
            return providers

    user = "root" if local_kvm else ctx.attr.exec_properties.get("test.dockerUser")
    if user not in ["root", "nobody"]:
        fail("unsupported local namespace test identity: %s" % user)
    docker = ctx.attr._local_test_backend[BuildSettingInfo].value == "docker"
    if docker and user != "root":
        fail("the Docker namespace fixture requires a root test identity")
    if initial_cgroup:
        if user != "root":
            fail("the initial cgroup namespace fixture requires a root test identity")
        docker = False
    if "no-local" in ctx.attr.tags:
        fail("local namespace test is tagged no-local")
    original_execution = None
    for provider in providers:
        if type(provider) == "ExecutionInfo":
            original_execution = provider
    requirements = dict(original_execution.requirements) if original_execution else {}
    if "no-local" in requirements:
        fail("local namespace test requires no-local")
    requirements["no-remote-exec"] = ""
    if initial_cgroup:
        # Docker's private cgroup namespace cannot create v1 hierarchies. Keep
        # this restriction on the TestRunner, independent of compilation.
        requirements["no-sandbox"] = ""

    result = []
    for provider in providers:
        if provider == original_execution:
            continue
        if user == "root" and type(provider) == "DefaultInfo":
            # Keep Bazel's run_under outside this executable. Docker tests use
            # their private cgroup hierarchy; host-local tests acquire root and
            # return output ownership to the unprivileged Bazel server.
            helper = ctx.attr._docker_setup if docker else ctx.attr._local_root
            helper_executable = ctx.executable._docker_setup if docker else ctx.executable._local_root
            executable = ctx.actions.declare_file(ctx.label.name + (".docker_setup" if docker else ".local_root"))
            ctx.actions.write(
                executable,
                "#!/bin/bash\n%s \"${TEST_SRCDIR}/${TEST_WORKSPACE}\"%s%s \"${TEST_SRCDIR}/${TEST_WORKSPACE}\"%s \"$@\"\n" % (
                    "exec sudo -n -E -- unshare --mount --propagation private --" if initial_cgroup else "exec",
                    shell.quote("/" + helper_executable.short_path),
                    " --initial-cgroup-namespace" if initial_cgroup else "",
                    shell.quote("/" + original_executable.short_path),
                ),
                is_executable = True,
            )
            helper_runfiles = ctx.runfiles(files = [executable, original_executable, helper_executable]).merge(
                helper[DefaultInfo].default_runfiles,
            )
            data_runfiles = provider.data_runfiles
            if data_runfiles == None:
                # Raw native_test returns the legacy runfiles-only form.
                data_runfiles = provider.default_runfiles
            provider = DefaultInfo(
                executable = executable,
                files = provider.files,
                default_runfiles = provider.default_runfiles.merge(helper_runfiles),
                data_runfiles = data_runfiles.merge(helper_runfiles),
            )
        result.append(provider)
    return result + [testing.ExecutionInfo(
        requirements = requirements,
        exec_group = original_execution.exec_group if original_execution else "test",
    )]

def _native_frontend_impl(ctx):
    return _route_test(ctx, ctx.super(), ctx.attr.exports[FrontendInfo].executable)

def _native_test_impl(ctx):
    providers = ctx.super()
    for provider in providers:
        if type(provider) == "DefaultInfo":
            # Skylib native_test explicitly returns only its executable in files.
            # Raw providers from ctx.super() have no files_to_run yet.
            [executable] = provider.files.to_list()
            return _route_test(ctx, providers, executable)
    fail("native_test did not return DefaultInfo")

_ROUTING_ATTRS = {
    "_docker_setup": attr.label(default = Label("//test/rbe:docker_setup"), executable = True, cfg = "target"),
    "_local_root": attr.label(default = Label("//test/rbe:local_root"), executable = True, cfg = "target"),
    "_local_test_architecture": attr.label(default = Label("//tools/bazeldefs:local_test_architecture")),
    "_local_test_backend": attr.label(default = Label("//tools/bazeldefs:local_test_backend")),
    "_local_test_requirements": attr.label(default = Label("//tools/bazeldefs:local_test_requirements")),
}

_native_frontend_test = rule(
    implementation = _native_frontend_impl,
    parent = frontend_test,
    attrs = _ROUTING_ATTRS,
)

# Extend native_test directly so its declared executable stays beside any
# sibling runtime resources. A with_cfg frontend relocates only the executable.
routed_native_test = rule(
    implementation = _native_test_impl,
    parent = _native_test,
    attrs = _ROUTING_ATTRS,
)

def with_test_architecture(test_rule, architecture, static = False, extra_providers = [], implicit_targets = None, test_frontend = _native_frontend_test):
    """Returns a with_cfg builder that preserves the test's other configuration."""
    target = _ARCHITECTURES[architecture]
    return with_cfg(
        test_rule,
        test_frontend = test_frontend,
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
