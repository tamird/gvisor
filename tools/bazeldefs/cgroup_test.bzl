"""Cgroup variants of the original test declarations."""

load("@with_cfg.bzl//:with_cfg.bzl", "frontend_test", "with_cfg")

# run_under is not a readable transition input in Bazel 8.5. Configure it only
# as an output on the actual test rule, rather than on an inner build target.
# Keep unrelated test environment values, including inherited client variables.
def _cgroup_v1_config(settings, _attr):
    return {
        "//tools/bazeldefs:cgroup_v1": True,
        "//command_line_option:cpu": "k8",
        "//command_line_option:platforms": [Label("@io_bazel_rules_go//go/toolchain:linux_amd64_cgo")],
        "//command_line_option:extra_execution_platforms": [
            str(Label("//tools/bazeldefs:rbe_linux_amd64_cgroup_v1")),
            str(Label("//tools/bazeldefs:rbe_linux_arm64")),
        ],
        "//command_line_option:run_under": "//test/rbe:cgroup_v1",
        "//command_line_option:test_env": [
            value
            for value in settings["//command_line_option:test_env"]
            if value.split("=", 1)[0] != "CGROUPV2"
        ] + ["CGROUPV2=false"],
    }

_cgroup_v1_transition = transition(
    implementation = _cgroup_v1_config,
    inputs = ["//command_line_option:test_env"],
    outputs = [
        "//tools/bazeldefs:cgroup_v1",
        "//command_line_option:cpu",
        "//command_line_option:platforms",
        "//command_line_option:extra_execution_platforms",
        "//command_line_option:run_under",
        "//command_line_option:test_env",
    ],
)

_cgroup_v1_frontend_test = rule(
    implementation = lambda ctx: ctx.super(),
    parent = frontend_test,
    cfg = _cgroup_v1_transition,
)

def with_cgroup_v1(test_rule, extra_providers = [], implicit_targets = None):
    """Reuses with_cfg's complete test forwarding with cgroup setup outside it."""
    return with_cfg(
        test_rule,
        test_frontend = _cgroup_v1_frontend_test,
        extra_providers = extra_providers,
        implicit_targets = implicit_targets,
    ).build()

def cgroup_v1_tags(tags):
    """Advertises the variant to the canonical qualification selector."""
    return tags + ["rbe-has-cgroup-v1-variant"]

def cgroup_v1_variant(name, test_rule, kwargs):
    """Declares the same test with an explicit, manual AMD64 cgroup profile."""
    attributes = dict(kwargs)
    attributes["tags"] = attributes.get("tags", []) + ["manual", "no-local"]
    attributes["target_compatible_with"] = attributes.get("target_compatible_with", []) + select({
        Label("//tools/bazeldefs:rbe"): [],
        "//conditions:default": [Label("@platforms//:incompatible")],
    })
    test_rule(name = name + "_cgroup_v1", **attributes)

def cgroup_v2_variant(name, test_rule, kwargs):
    """Declares the same test with a rule-owned cgroup-v2 environment."""

    # Preserve the ordinary target's environment for Make's CGROUPV2 override.
    attributes = dict(kwargs)
    attributes["tags"] = attributes.get("tags", []) + ["manual"]
    attributes["env"] = dict(attributes.get("env", {}), CGROUPV2 = "true")
    test_rule(name = name + "_cgroup_v2", **attributes)
