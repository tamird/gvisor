load("@rules_python//python:py_library.bzl", "py_library")
load("@rules_python//python:py_test.bzl", "py_test")
load("//tools:defs.bzl", "requires_cap_sys_admin")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

py_library(
    name = "sandbox_py",
    srcs = [
        "gvisor/__init__.py",
        "gvisor/sandbox.py",
    ],
    imports = ["."],
    visibility = ["//:__subpackages__"],
)

exports_files(
    ["gvisor/sandbox.py"],
    visibility = ["//:__subpackages__"],
)

py_test(
    name = "sandbox_py_test",
    srcs = ["tests/sandbox_test.py"],
    data = ["//:release"],
    exec_properties = requires_cap_sys_admin(),
    main = "tests/sandbox_test.py",
    tags = ["nogotsan"],
    deps = [
        ":sandbox_py",
    ],
)
