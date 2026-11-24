load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
load("//tools:defs.bzl", "go_library")

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

# Sets up the existing host daemon, then executes an ordinary benchmark caller.
sh_binary(
    name = "host_docker",
    srcs = ["host_docker.sh"],
    data = [
        "//:release",
        "//images:benchmark_alpine_load",
    ],
    tags = ["manual"],
    visibility = ["//:sandbox"],
)

sh_binary(
    name = "startup_host_docker",
    srcs = ["host_docker.sh"],
    data = [
        "//:release",
        "//images:benchmark_alpine_load",
        "//images:benchmark_nginx_load",
        "//images:benchmark_node_load",
        "//images:benchmark_util_load",
        "//images/basic:benchmark_redis_load",
    ],
    tags = ["manual"],
    target_compatible_with = ["@platforms//cpu:x86_64"],
    visibility = ["//:sandbox"],
)
