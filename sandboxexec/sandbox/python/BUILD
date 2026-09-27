# Placeholder: load py_library
# Placeholder: load py_test
load("//tools:defs.bzl", "namespace_test_exec_properties")

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
    # The binding starts the runtime, including namespaces and cgroups.
    exec_properties = namespace_test_exec_properties(),
    main = "tests/sandbox_test.py",
    tags = ["nogotsan"],
    deps = [
        ":sandbox_py",
    ],
)
