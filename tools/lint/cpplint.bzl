"""Package-local C++ lint checks, sharing the tool and policy with tools/lint.sh."""

load("@bazel_skylib//rules:native_binary.bzl", "native_test")

CPPLINT_FLAGS = ["--quiet", "--repository=.", "--filter=-,+build/include_order,+build/c++11"]

def cpplint_test():
    """Checks C/C++ files in the calling package, including nested nonpackages."""
    srcs = native.glob(["**/*.c", "**/*.cc", "**/*.h"], allow_empty = True)
    if not srcs:
        return
    native_test(
        name = "cpplint_test",
        src = "@cpplint//:cpplint",
        data = srcs,
        args = CPPLINT_FLAGS + ["$(rootpath :%s)" % src for src in srcs],
        size = "medium",
        tags = ["manual"],
    )
