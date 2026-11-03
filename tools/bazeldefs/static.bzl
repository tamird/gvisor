"""Rules for static binaries using the LLVM toolchain's musl sysroot."""

load("@io_bazel_rules_go//go:def.bzl", "GoArchive", "GoLibrary", _go_binary = "go_binary", _go_test = "go_test")
load("@rules_cc//cc:defs.bzl", _cc_binary = "cc_binary")
load("@with_cfg.bzl//:with_cfg.bzl", "with_cfg")
load("//tools/bazeldefs:defs.bzl", "select_arch")

# LLVM's GNU sysroot contains dynamic linking stubs, not static libc archives.
# Compile and link static targets and their dependencies against the same
# musl sysroot.
_MUSL_PLATFORMS = select_arch(
    amd64 = [Label("@llvm//platforms:linux_x86_64_musl")],
    arm64 = [Label("@llvm//platforms:linux_aarch64_musl")],
)

musl_cc_binary, _cc_binary_transition = with_cfg(_cc_binary, executable = True).set("platforms", _MUSL_PLATFORMS).build()

_musl_go_binary, _go_binary_transition = with_cfg(
    _go_binary,
    executable = True,
    extra_providers = [GoLibrary, GoArchive],
).set("platforms", _MUSL_PLATFORMS).build()

def go_binary(name, basename = None, tags = [], visibility = None, **kwargs):
    """Build a static Go executable in the usual rules_go runfiles layout."""
    # with_cfg places the executable below its target name. Use the rules_go
    # <name>_/<basename> path; otherwise lookups of <name> find a directory.
    _musl_go_binary(
        name = name + "_",
        basename = basename or name.rpartition("/")[2],
        tags = tags + ["manual"],
        visibility = ["//visibility:private"],
        **kwargs
    )
    native.alias(
        name = name,
        actual = ":" + name + "_",
        tags = tags,
        testonly = kwargs.get("testonly"),
        visibility = visibility,
    )

# Extend the upstream test rule rather than wrapping its executable. Exporting
# it as go_test preserves the rule kind used by the Nogo aspect, together with
# the original test attributes, providers, and Go configuration transition.
go_test, _go_test_transition = with_cfg(_go_test).set("platforms", _MUSL_PLATFORMS).build()
