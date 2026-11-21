load("//tools:defs.bzl", "build_test", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "harness",
    testonly = 1,
    srcs = [
        "docker_worker_linux.go",
        "docker_worker_unsupported.go",
        "harness.go",
        "machine.go",
        "util.go",
    ],
    importpath = "gvisor.dev/gvisor/test/benchmarks/harness",
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/cleanup",
        "//pkg/test/dockerutil",
        "//pkg/test/testutil",
        "@com_github_docker_docker//api/types/image:go_default_library",
        "@com_github_docker_docker//api/types/mount:go_default_library",
        "@com_github_docker_docker//client:go_default_library",
    ],
)

go_test(
    name = "docker_worker_test",
    size = "small",
    timeout = "moderate",
    srcs = ["docker_worker_test.go"],
    args = ["--alpine-archive=$(rootpath //images/basic:alpine_archive)"],
    data = ["//images/basic:alpine_archive"],
    embed = [":harness"],
    exec_properties = {
        "test.init-dockerd": "false",
        "test.workload-isolation-type": "firecracker",
    },
    pure = True,
    tags = ["manual"],
    target_compatible_with = ["@platforms//os:linux"],
    deps = [
        "//pkg/test/testutil",
        "@com_github_docker_docker//api/types/container:go_default_library",
        "@com_github_docker_docker//api/types/mount:go_default_library",
        "@com_github_docker_docker//api/types/network:go_default_library",
        "@com_github_docker_docker//client:go_default_library",
        "@com_github_docker_docker//errdefs:go_default_library",
    ],
)

# Select the entrypoint by producer metadata while retaining the complete
# release fileset in the runtime test's data.
filegroup(
    name = "runsc_entrypoint",
    srcs = ["//:release"],
    output_group = "runsc",
    visibility = ["//test/benchmarks/network:__pkg__"],
)

build_test(
    name = "runsc_release_build_test",
    tags = ["manual"],
    targets = ["//:release"],
)

go_test(
    name = "runsc_worker_test",
    size = "small",
    timeout = "moderate",
    srcs = ["docker_worker_test.go"],
    args = [
        "--alpine-archive=$(rootpath //images/basic:alpine_archive)",
        "--worker-runtime=runsc",
        "--release-runsc=$(rootpath :runsc_entrypoint)",
    ],
    data = [
        ":runsc_entrypoint",
        "//:release",
        "//images/basic:alpine_archive",
    ],
    embed = [":harness"],
    exec_properties = {
        "test.init-dockerd": "false",
        "test.workload-isolation-type": "firecracker",
    },
    pure = True,
    tags = ["manual"],
    target_compatible_with = ["@platforms//os:linux"],
    deps = [
        "//pkg/test/testutil",
        "@com_github_docker_docker//api/types/container:go_default_library",
        "@com_github_docker_docker//api/types/mount:go_default_library",
        "@com_github_docker_docker//api/types/network:go_default_library",
        "@com_github_docker_docker//client:go_default_library",
        "@com_github_docker_docker//errdefs:go_default_library",
    ],
)

