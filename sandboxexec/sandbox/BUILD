load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

# gazelle:exclude sandbox_bench_test.go

go_library(
    name = "sandbox",
    srcs = [
        "debug.go",
        "oci.go",
        "sandbox.go",
        "storage.go",
    ],
    importpath = "gvisor.dev/gvisor/sandboxexec/sandbox",
    visibility = ["//:__subpackages__"],
    deps = [
        "//pkg/abi/linux",
        "//pkg/log",
        "//pkg/sighandling",
        "//runsc/specutils",
        "@com_github_opencontainers_runtime_spec//specs-go:go_default_library",
    ],
)

go_test(
    name = "sandbox_test",
    srcs = [
        "oci_test.go",
        "sandbox_test.go",
        "storage_test.go",
    ],
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
    tags = ["nogotsan"],
    deps = [
        ":sandbox",
        "//pkg/test/testutil",
        "@com_github_opencontainers_runtime_spec//specs-go:go_default_library",
    ],
)

# Benchmarks for the sandboxexec Go bindings.
go_test(
    name = "sandbox_benchmark_test",
    size = "large",
    srcs = ["sandbox_bench_test.go"],
    args = [
        "-test.bench=.",
        "-test.run=^$$",
        "-test.benchtime=30x",  # A fixed iteration count.
        "-test.v",
    ],
    data = ["//:release"],
    tags = [
        "manual",
        "nogotsan",
        "notap",
    ],
    deps = [
        ":sandbox",
        "//pkg/test/testutil",
    ],
)
