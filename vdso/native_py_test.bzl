"""The Python test rule, for with_cfg's incoming rule transition."""

load("@rules_python//python/api:executables.bzl", "executables")

native_py_test = executables.py_test_rule_builder().build()
