"""Go rules with static linking and coverage configurations."""

load("@io_bazel_rules_go//go:def.bzl", "GoArchive", "GoLibrary", _go_binary = "go_binary", _go_test = "go_test")
load("@with_cfg.bzl//:with_cfg.bzl", "with_cfg")
load("//tools/bazeldefs:defs.bzl", "select_arch")

# LLVM's GNU sysroot contains dynamic linking stubs, not static libc archives.
# Change the platform for static targets and their dependencies so that cgo
# compiles and links against the same musl sysroot.
_MUSL_PLATFORMS = select_arch(
    amd64 = [Label("@llvm//platforms:linux_x86_64_musl")],
    arm64 = [Label("@llvm//platforms:linux_aarch64_musl")],
)

# Keep the existing coverage exclusions: low-level context switching and the
# coverage runtime cannot be instrumented, and atomic counters do not respect
# go:norace in the synchronization packages (https://go.dev/issue/43007).
# Instrumenting the BPF optimizer also makes sandbox startup prohibitively
# expensive in race builds.
# Prefix matching intentionally excludes descendants as well as the package.
_COVERAGE_FILTER = "^//,-//pkg/(sentry/platform|ring0|coverage|sleep|sync|syncevent|bpf)"

def _go_binary_variant(static = False, coverage = False):
    # Compose settings before building one wrapper around the upstream macro.
    # Nesting with_cfg macro wrappers recurses into the same Starlark function.
    configured = with_cfg(
        _go_binary,
        executable = True,
        extra_providers = [GoLibrary, GoArchive],
    )
    if static:
        configured = configured.set("platforms", _MUSL_PLATFORMS)
    if coverage:
        # rules_go supplies atomic runtime/coverage instrumentation with the
        # declared SDK. Direct imports need no cmd/go internal-import patch.
        configured = configured.set("collect_code_coverage", True).set(
            "instrumentation_filter",
            _COVERAGE_FILTER,
        )
    return configured.build()

go_binary, _go_binary_transition = _go_binary_variant(static = True)
go_cov, _go_cov_transition = _go_binary_variant(coverage = True)
static_go_cov, _static_go_cov_transition = _go_binary_variant(static = True, coverage = True)

# Extend the upstream test rule rather than wrapping its executable. Exporting
# it as go_test preserves the rule kind used by the Nogo aspect, together with
# the original test attributes, providers, and Go configuration transition.
go_test, _go_test_transition = with_cfg(_go_test).set("platforms", _MUSL_PLATFORMS).build()
