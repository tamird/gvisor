"""The underlying rules_python test rule used by architecture transitions."""

load("@rules_python//python/api:executables.bzl", "executables")

py_test = executables.py_test_rule_builder().build()
