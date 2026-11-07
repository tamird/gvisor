load("//tools/bazeldefs:test_architectures.bzl", "test_architecture_variants", "with_test_architecture")
load(":native_py_test.bzl", "native_py_test")

_amd64, _amd64_internal = with_test_architecture(native_py_test, "amd64").build()
_arm64, _arm64_internal = with_test_architecture(native_py_test, "arm64").build()

def mixed_abi_tests(name, **kwargs):
    test_architecture_variants(name, ["amd64", "arm64"], {"amd64": _amd64, "arm64": _arm64}, kwargs)
