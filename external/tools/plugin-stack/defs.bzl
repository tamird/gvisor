"""Target-owned configuration for the existing TLDK plugin consumers."""

load("@io_bazel_rules_go//go:def.bzl", _go_test = "go_test")
load("@with_cfg.bzl//:with_cfg.bzl", "with_cfg")
load("//tools/bazeldefs:go_runtime.bzl", "go_runtime_test")

def _plugin_configuration(configured):
    # Bazel forbids transitioning --define. One build setting replaces the
    # plugin_tldk/network_plugins pair for both the Go and supplier selects.
    return configured.set(Label("//external/tools/plugin-stack:tldk"), True).set(
        Label("@io_bazel_rules_go//go/config:pure"),
        False,
    )

# Keep one configuration owner for compilation and runtime dependencies. The
# raw rule retains go_test's kind for the package and Nogo aspects.
go_test, _plugin_test_transition = _plugin_configuration(with_cfg(_go_test)).build()
_plugin_runtime_test, _plugin_runtime_transition = _plugin_configuration(with_cfg(go_runtime_test)).build()
plugin_test_rules = struct(compilation = go_test, runtime = _plugin_runtime_test)

# Only the public build lane uses opt and Bazel's default stripping policy.
# Network tests keep their existing compilation mode and strip=never, including
# the complete plugin release fileset and sidecars below their configured rule.
plugin_build, _plugin_build_transition = _plugin_configuration(with_cfg(native.filegroup)).set(
    "compilation_mode",
    "opt",
).set("strip", "sometimes").build()
