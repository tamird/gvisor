load("//test/docker:config.bzl", "INTEGRATION_VARIANTS")
load("//test/docker:defs.bzl", "docker_test")
load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

docker_test(
    name = "integration_test",
    size = "large",
    srcs = [
        "exec_test.go",
        "integration_test.go",
        "lifecycle_test.go",
        "main_test.go",
    ],
    cohort = "integration",

    # Needed to invalidate the bazel cache in case of any code changes.
    data = ["//:release"],
    library = ":integration",
    runtime_variants = INTEGRATION_VARIANTS,
    tags = ["manual"],
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/abi/linux",
        "//pkg/sentry/kernel/auth",
        "//pkg/test/dockerutil",
        "//pkg/test/testutil",
        "//runsc/specutils",
        "@com_github_docker_docker//api/types/mount:go_default_library",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

docker_test(
    name = "uds_proxy_test",
    cohort = "uds_proxy",
    size = "large",
    srcs = [
        "uds_proxy_test.go",
    ],
    # Needed to invalidate the bazel cache in case of any code changes.
    data = ["//:release"],
    library = ":integration",
    tags = ["manual"],
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/tcpip",
        "//pkg/tcpip/adapters/gonet",
        "//pkg/tcpip/header",
        "//pkg/tcpip/link/fdbased",
        "//pkg/tcpip/network/ipv4",
        "//pkg/tcpip/stack",
        "//pkg/tcpip/transport/tcp",
        "//pkg/test/dockerutil",
        "//pkg/test/testutil",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

docker_test(
    name = "integration_runtime_test",
    size = "large",
    srcs = [
        "integration_runtime_test.go",
        "main_test.go",
        "systemd_test.go",
    ],
    cohort = "runtime",
    # Needed to invalidate the bazel cache in case of any code changes.
    data = ["//:release"],
    library = ":integration",
    tags = ["manual"],
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/cleanup",
        "//pkg/fsutil",
        "//pkg/test/dockerutil",
        "//pkg/test/testutil",
        "@com_github_docker_docker//api/types/mount:go_default_library",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

docker_test(
    name = "runtime_in_docker_test",
    size = "large",
    srcs = [
        "main_test.go",
        "runtime_in_docker_test.go",
    ],
    cohort = "nested_runtime",
    # Needed to invalidate the bazel cache in case of any code changes.
    data = ["//:release"],
    library = ":integration",
    tags = ["manual"],
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/test/dockerutil",
        "//pkg/test/testutil",
        "@com_github_docker_docker//api/types/mount:go_default_library",
    ],
)

go_test(
    name = "podman_test",
    size = "large",
    srcs = [
        "podman_test.go",
    ],
    # runsc is needed to invalidate the bazel cache in case of any code changes.
    data = ["//:release"],
    library = ":integration",
    # Requires docker and runsc to be configured before the test runs.
    local = True,
    tags = ["manual"],
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/test/dockerutil",
        "//pkg/test/testutil",
    ],
)

go_library(
    name = "integration",
    srcs = ["integration.go"],
)
