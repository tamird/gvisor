"""Package-local C++ lint checks, sharing the tool and policy with tools/lint.sh."""

load("@bazel_skylib//rules:native_binary.bzl", "native_test")

CPPLINT_FLAGS = ["--quiet", "--repository=.", "--filter=-,+build/include_order,+build/c++11"]

def cc_lint_tests():
    """Checks C/C++ files in the calling package, including nested nonpackages."""
    srcs = native.glob(["**/*.c", "**/*.cc", "**/*.h"], allow_empty = True)
    if not srcs:
        return
    args = ["$(rootpath :%s)" % src for src in srcs]
    native_test(
        name = "cpplint_test",
        src = "@cpplint",
        data = srcs,
        args = CPPLINT_FLAGS + args,
        size = "medium",
        tags = ["manual"],
    )
    native_test(
        name = "clang_format_test",
        src = "@llvm//tools:clang-format",
        # The LLVM driver selects clang-format from the executable name.
        out = "bin/clang-format",
        data = srcs + ["//:.clang-format"],
        args = ["--dry-run", "--Werror", "--style=file:$(rootpath //:.clang-format)"] + args,
        size = "small",
        tags = ["manual"],
    )
