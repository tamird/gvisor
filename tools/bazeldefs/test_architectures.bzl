"""Architecture variants of maintained test declarations."""

load("@with_cfg.bzl//:with_cfg.bzl", "with_cfg")

_ARCHITECTURES = {
    "amd64": struct(
        cpu = "k8",
        constraint = Label("@platforms//cpu:x86_64"),
        platform = Label("//tools/bazeldefs:linux_amd64"),
    ),
    "arm64": struct(
        cpu = "aarch64",
        constraint = Label("@platforms//cpu:aarch64"),
        platform = Label("//tools/bazeldefs:linux_arm64"),
    ),
}

def with_test_architecture(test_rule, architecture, extra_providers = [], implicit_targets = None):
    """Returns a with_cfg builder that preserves the test's other configuration."""
    target = _ARCHITECTURES[architecture]
    return with_cfg(test_rule, extra_providers = extra_providers, implicit_targets = implicit_targets).set("cpu", target.cpu).set(
        "platforms",
        [target.platform],
    )

arm64_test_files, _arm64_test_files_transition = with_test_architecture(native.filegroup, "arm64").build()

def _test_attributes(architecture, kwargs):
    attributes = dict(kwargs)
    # Only test execution requires the target CPU. Compilers and linkers may
    # run in another execution group on a different architecture.
    groups = dict(attributes.get("exec_group_compatible_with") or {})
    groups["test"] = groups.get("test", []) + [
        Label("@platforms//os:linux"),
        _ARCHITECTURES[architecture].constraint,
    ]
    attributes["exec_group_compatible_with"] = groups
    return attributes

def arm64_presubmit_test(name, test_rule, kwargs):
    """Adds an ARM64 test when the presubmit also runs the original on AMD64."""
    attributes = _test_attributes("arm64", kwargs)
    attributes["target_compatible_with"] = (attributes.get("target_compatible_with") or []) + select({
        Label("//test/rbe:mixed_native_tests"): [],
        "//conditions:default": [Label("@platforms//:incompatible")],
    })
    test_rule(name = name + "_presubmit_arm64", **attributes)

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
        attributes = _test_attributes(architecture, kwargs)
        attributes["tags"] = attributes.get("tags", []) + ["manual"]
        test_rules[architecture](name = name + "_" + architecture, **attributes)
