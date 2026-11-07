"""Source-presubmit variants of test rules provided by external modules."""

load("@bazel_skylib//rules:build_test.bzl", _build_test = "build_test")
load("@bazel_skylib//rules:native_binary.bzl", _native_test = "native_test")
load("@rules_shell//shell:sh_test.bzl", _sh_test = "sh_test")
load(":test_architectures.bzl", "with_presubmit_variant")

native_test = with_presubmit_variant(_native_test)
sh_test = with_presubmit_variant(_sh_test)

_presubmit_build_test = with_presubmit_variant(_build_test)

def build_test(name, size = "small", **kwargs):
    """Applies Skylib's small default to the public test on each architecture."""
    _presubmit_build_test(name = name, size = size, **kwargs)
