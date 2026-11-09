"""Source-presubmit variants of test rules provided by external modules."""

load("@bazel_skylib//rules:build_test.bzl", _build_test = "build_test")
load("@bazel_skylib//rules:native_binary.bzl", _native_test = "native_test")
load("@rules_shell//shell:sh_test.bzl", _sh_test = "sh_test")
load(":test_architectures.bzl", "arm64_presubmit_test", "with_presubmit_variant", "with_test_architecture")

_arm64_native_test, _native_transition = with_test_architecture(_native_test, "arm64").build()
_arm64_sh_test, _sh_transition = with_test_architecture(_sh_test, "arm64").build()

sh_test = with_presubmit_variant(_sh_test, _arm64_sh_test)

def native_test(name, out = "", **kwargs):
    """Preserves the original output name and gives the ARM64 companion its own executable."""
    _native_test(name = name, out = out, **kwargs)
    arm64_presubmit_test(name, _arm64_native_test, kwargs)

def arm64_presubmit_executable(name, **kwargs):
    """Runs a generated test executable and its runfiles in the ARM64 configuration."""
    arm64_presubmit_test(name, _arm64_native_test, dict(kwargs, src = ":" + name))

def build_test(name, targets, size = "small", **kwargs):
    """Builds Skylib's declared inputs on both architectures."""
    _build_test(name = name, targets = targets, size = size, **kwargs)
    arm64_presubmit_executable(name, size = size, **kwargs)
