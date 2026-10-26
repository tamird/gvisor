load("//tools:defs.bzl", "go_library", "go_test", "namespace_test_exec_properties", "native_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

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
        "//runsc/config",
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
    # The runtime creates namespaces and configures cgroups.
    exec_properties = namespace_test_exec_properties(),
    tags = ["nogotsan"],
    visibility = ["//test/rbe:__pkg__"],
    deps = [
        ":sandbox",
        "//pkg/test/testutil",
        "@com_github_opencontainers_runtime_spec//specs-go:go_default_library",
    ],
)

# Check the existing rejection before sandbox networking acquires privileges.
native_test(
    name = "sandbox_network_nonroot_test",
    src = ":sandbox_test",
    args = ["-test.run=^TestNonRootNetworkingError$$"],
    env = {
        "GO_TEST_RUN_FROM_BAZEL": "1",
        "GO_TEST_WRAP_TESTV": "1",
    },
    exec_properties = namespace_test_exec_properties(user = "nobody"),
    tags = [
        "manual",
        "nogotsan",
    ],
    visibility = ["//test/rbe:__pkg__"],
)
