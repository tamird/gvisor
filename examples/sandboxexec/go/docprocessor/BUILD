load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "docprocessor",
    srcs = ["docprocessor.go"],
    visibility = ["//examples/sandboxexec/go:__subpackages__"],
    deps = ["//sandboxexec/sandbox"],
)

go_test(
    name = "docprocessor_test",
    srcs = ["docprocessor_test.go"],
    data = ["//:release"],
    embed = [":docprocessor"],
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
    tags = ["nogotsan"],
    deps = ["//pkg/test/testutil"],
)
