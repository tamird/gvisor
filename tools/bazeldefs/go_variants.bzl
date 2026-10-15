"""Go rules with the qualified static-link platform transition."""

load("@io_bazel_rules_go//go:def.bzl", "GoArchive", "GoLibrary", _go_binary = "go_binary")
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
