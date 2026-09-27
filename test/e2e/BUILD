load("@rules_oci//oci:defs.bzl", "oci_load")
load("//tools:defs.bzl", "go_library", "go_test")
load("//tools/bazeldefs:platforms.bzl", "docker_test_exec_properties")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_test(
    name = "integration_test",
    size = "large",
    srcs = [
        "exec_test.go",
        "integration_test.go",
        "lifecycle_test.go",
    ],

    # Needed to invalidate the bazel cache in case of any code changes.
    data = ["//:release"],
    library = ":integration",
    # Requires docker and runsc to be configured before the test runs.
    local = True,
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

oci_load(
    name = "lifecycle_image",
    image = "@docker_test_nginx",
    repo_tags = ["gvisor.dev/images/basic/nginx:latest"],
    tags = ["manual"],
)

filegroup(
    name = "lifecycle_image_tar",
    srcs = [":lifecycle_image"],
    output_group = "tarball",
    tags = ["manual"],
)

go_test(
    name = "docker_lifecycle_test",
    # The action owns dockerd/containerd as well as runsc and nginx.
    size = "enormous",
    srcs = ["lifecycle_test.go"],
    args = ["--docker_image_archive=$(rootpath :lifecycle_image_tar)"],
    data = [
        ":lifecycle_image_tar",
        "//:release",
    ],
    exec_properties = docker_test_exec_properties(),
    library = ":integration",
    # Nogo always checks both architectures. integration_test_nogo analyzes
    # the same source without the AMD64-only image dependency.
    nogo = False,
    rundir = ".",
    tags = ["manual"],
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    deps = [
        "//pkg/test/dockerutil",
        "//pkg/test/testutil",
    ],
)

go_test(
    name = "integration_runtime_test",
    size = "large",
    srcs = [
        "integration_runtime_test.go",
        "systemd_test.go",
    ],
    # Needed to invalidate the bazel cache in case of any code changes.
    data = ["//:release"],
    library = ":integration",
    # Requires docker and runsc to be configured before the test runs.
    local = True,
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

go_test(
    name = "runtime_in_docker_test",
    size = "large",
    srcs = [
        "runtime_in_docker_test.go",
    ],
    # Needed to invalidate the bazel cache in case of any code changes.
    data = ["//:release"],
    library = ":integration",
    # Requires docker and runsc to be configured before the test runs.
    local = True,
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
