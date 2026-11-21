load("@rules_python//python:py_library.bzl", "py_library")
load("@rules_python//python:py_test.bzl", "py_test")

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
    # Use BuildBuddy's microVM runner for sandbox creation:
    # https://www.buildbuddy.io/docs/rbe-microvms/
    # Its default OCI runner omits CAP_SYS_ADMIN:
    # https://github.com/buildbuddy-io/buildbuddy/blob/168813c94525e5106459531fb629429d5fe2fb60/enterprise/server/remote_execution/containers/ociruntime/ociruntime.go#L157-L173
    # Disable external networking, which Firecracker enables by default:
    # https://www.buildbuddy.io/docs/rbe-platforms/#runner-container-support
    exec_properties = {
        "test.network": "off",
        "test.workload-isolation-type": "firecracker",
    },
    main = "tests/sandbox_test.py",
    tags = ["nogotsan"],
    deps = [
        ":sandbox_py",
    ],
)
