"""Target-owned configuration for the existing TLDK plugin consumers."""

load("@io_bazel_rules_go//go:def.bzl", _go_test = "go_test")
load("@with_cfg.bzl//:with_cfg.bzl", "with_cfg")

def _plugin_configuration(configured):
    # Bazel forbids transitioning --define. One build setting replaces the
    # plugin_tldk/network_plugins pair for both the Go and supplier selects.
    return configured.set(Label("//external/tools/plugin-stack:tldk"), True).set(
        Label("@io_bazel_rules_go//go/config:pure"),
        False,
    )

# Extend the loaded raw rule so the existing Docker/Go declarations keep their
# attributes, runfiles, providers and execution properties. The Go macro also
# passes this rule to its native architecture variants.
# Keep the go_test rule kind used by the existing Nogo aspect.
go_test, _plugin_test_transition = _plugin_configuration(with_cfg(_go_test)).build()

# Only the public build lane uses opt and Bazel's default stripping policy.
# Network tests keep their existing compilation mode and strip=never, including
# the complete plugin release fileset and sidecars below their configured rule.
plugin_build, _plugin_build_transition = _plugin_configuration(with_cfg(native.filegroup)).set(
    "compilation_mode",
    "opt",
).set("strip", "sometimes").build()
