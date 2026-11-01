"""Static Go rules using the LLVM toolchain's musl sysroot."""

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

go_binary, _go_binary_transition = with_cfg(
    _go_binary,
    executable = True,
    extra_providers = [GoLibrary, GoArchive],
).set("platforms", _MUSL_PLATFORMS).build()

# Extend the upstream test rule rather than wrapping its executable. Exporting
# it as go_test preserves the rule kind used by the Nogo aspect, together with
# the original test attributes, providers, and Go configuration transition.
go_test, _go_test_transition = with_cfg(_go_test).set("platforms", _MUSL_PLATFORMS).build()

_covered_binary = with_cfg(
    _go_binary,
    executable = True,
    extra_providers = [GoLibrary, GoArchive],
).set("collect_code_coverage", True).set(
    "instrumentation_filter",
    # Exclude the coverage runtime itself and low-level platform code. In
    # sleep, sync, and syncevent, //go:norace doesn't stop coverage from
    # inserting atomic counter updates. Under -race those atomics call
    # into the race detector where it cannot safely run. Reconsider these
    # exclusions when Go can suppress those calls:
    # https://github.com/golang/go/issues/43007
    #
    # Race builds need different seccomp filters. They build them at
    # startup so ordinary runsc binaries needn't embed race-only filters.
    # bpf.Optimize restarts its passes after each change. Instrumenting
    # that loop sends each block's atomic counter update through the race
    # detector and can add tens of seconds to sandbox startup.
    "^//,-//pkg/(sentry/platform|ring0|coverage|sleep|sync|syncevent|bpf)",
)

go_cov, _go_cov_transition = _covered_binary.build()
static_go_cov, _static_go_cov_transition = _covered_binary.clone().set("platforms", _MUSL_PLATFORMS).build()
