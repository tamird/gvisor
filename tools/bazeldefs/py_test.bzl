"""Python tests that exercise target-specific artifacts in the mixed presubmit."""

load("@rules_python//python:py_test.bzl", _py_test = "py_test")
load(":py_test_rule.bzl", "py_test_rule")
load(":test_architectures.bzl", "arm64_presubmit_test", "with_test_architecture")

_arm64_test, _arm64_transition = with_test_architecture(py_test_rule, "arm64").build()

def py_test(name, **kwargs):
    """Run the original test and its ARM64 companion in the mixed presubmit."""
    _py_test(name = name, **kwargs)
    arm64_presubmit_test(name, _arm64_test, kwargs)
