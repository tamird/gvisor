load("@rules_python//python:py_test.bzl", "py_test")
load("//tools/bazeldefs:test_architectures.bzl", "test_architecture_variants", "with_test_architecture")

_amd64, _amd64_internal = with_test_architecture(py_test, "amd64").build()
_arm64, _arm64_internal = with_test_architecture(py_test, "arm64").build()

def mixed_abi_tests(name, **kwargs):
    test_architecture_variants(name, ["amd64", "arm64"], {"amd64": _amd64, "arm64": _arm64}, kwargs)
