"""Package-local C++ lint checks, sharing the tool and policy with tools/lint.sh."""

load("@rules_python//python:py_binary.bzl", "py_binary")
load("@rules_python//python:py_test.bzl", "py_test")

_CPPLINT = "@cpplint//:cpplint.py"
_FLAGS = ["--quiet", "--repository=.", "--filter=-,+build/include_order,+build/c++11"]

def cpplint_binary(name):
    """The same checker for files not covered by a package test."""
    py_binary(
        name = name,
        srcs = [_CPPLINT],
        main = _CPPLINT,
        args = _FLAGS,
        env = {"PYTHONWARNINGS": "ignore::DeprecationWarning"},
    )

def cpplint_test():
    """Checks C/C++ files in the calling package, including nested nonpackages."""
    srcs = native.glob(["**/*.c", "**/*.cc", "**/*.h"], allow_empty = True)
    if not srcs:
        fail("cpplint_test has no C/C++ sources in " + native.package_name())
    py_test(
        name = "cpplint_test",
        srcs = [_CPPLINT],
        main = _CPPLINT,
        data = srcs,
        args = _FLAGS + ["$(rootpath :%s)" % src for src in srcs],
        env = {"PYTHONWARNINGS": "ignore::DeprecationWarning"},
        size = "medium",
        tags = ["manual"],
    )
