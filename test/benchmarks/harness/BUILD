load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "harness",
    testonly = 1,
    srcs = [
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
        "@com_github_docker_docker//api/types/mount:go_default_library",
    ],
)

go_test(
    name = "docker_worker_test",
    size = "small",
    timeout = "moderate",
    srcs = ["docker_worker_test.go"],
    args = ["--alpine-archive=$(rootpath //images/basic:alpine_archive)"],
    data = ["//images/basic:alpine_archive"],
    exec_properties = {
        "test.init-dockerd": "true",
        "test.workload-isolation-type": "firecracker",
    },
    pure = True,
    tags = ["manual"],
    deps = [
        "//pkg/test/testutil",
        "@com_github_docker_docker//api/types/container:go_default_library",
        "@com_github_docker_docker//api/types/mount:go_default_library",
        "@com_github_docker_docker//client:go_default_library",
        "@com_github_docker_docker//errdefs:go_default_library",
    ],
)
