load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
load("//tools:defs.bzl", "build_test", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

sh_binary(
    name = "native_worker",
    srcs = ["native_worker.sh"],
    tags = ["manual"],
    target_compatible_with = ["@platforms//os:linux"],
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


# Each category runs the shared-daemon ABBA loop with its full leaf set.
# Benchtime must be supplied before selecting the paired test.
_PAIRED_BENCHMARKS = [
    (
        "iperf",
        "//test/benchmarks/network:iperf_test",
        8,
        [
            "//images:benchmark_iperf_archive",
        ],
    ),
    (
        "nginx",
        "//test/benchmarks/network:nginx_test",
        72,
        [
            "//images:benchmark_nginx_archive",
            "//images:benchmark_hey_archive",
            "//images:benchmark_util_archive",
        ],
    ),
    (
        "httpd",
        "//test/benchmarks/network:httpd_test",
        42,
        [
            "//images:benchmark_httpd_archive",
            "//images:benchmark_hey_archive",
            "//images:benchmark_util_archive",
        ],
    ),
    (
        "node",
        "//test/benchmarks/network:node_test",
        4,
        [
            "//images:benchmark_node_archive",
            "//images/basic:benchmark_redis_archive",
            "//images:benchmark_hey_archive",
            "//images:benchmark_util_archive",
        ],
    ),
    (
        "ruby",
        "//test/benchmarks/network:ruby_test",
        4,
        [
            "//images:benchmark_ruby_archive",
            "//images/basic:benchmark_redis_archive",
            "//images:benchmark_hey_archive",
            "//images:benchmark_util_archive",
        ],
    ),
    (
        "redis",
        "//test/benchmarks/database:redis_test",
        20,
        [
            "//images/basic:benchmark_redis_archive",
        ],
    ),
]

[
    go_test(
        name = "paired_" + category + "_worker_test",
        size = "large",
        timeout = "long",
        srcs = [
            "paired_products_test.go",
            "paired_worker_test.go",
        ],
        args = [
            "--paired-base-runsc=$(rootpath :runsc_entrypoint)",
            "--paired-candidate-runsc=$(rootpath :runsc_entrypoint)",
            "--paired-base-source=e50582f1801aec027ccbc427d34ecae10d393bf9",
            "--paired-candidate-source=e50582f1801aec027ccbc427d34ecae10d393bf9",
            "--paired-benchmark=$(rootpath %s)" % binary,
            "--paired-leaves=%d" % leaves,
            "--paired-image-archives='%s'" % " ".join(["$(rootpaths %s)" % archive for archive in archives]),
            "--client-cpuset=0-1",
            "--server-cpuset=2-3",
            "-test.run=^$$",
        ],
        data = [
            ":runsc_entrypoint",
            "//:release",
            binary,
        ] + archives,
        embed = [":harness"],
        exec_properties = {
            "test.EstimatedCPU": "4",
            "test.EstimatedMemory": "2G" if category == "ruby" else "500M",
            "test.init-dockerd": "false",
            "test.workload-isolation-type": "firecracker",
        },
        pure = True,
        tags = ["manual"],
        target_compatible_with = [
            "@platforms//cpu:x86_64",
            "@platforms//os:linux",
        ],
        deps = [
            "//pkg/test/testutil",
            "@org_golang_x_sys//unix:go_default_library",
            "@com_github_docker_docker//api/types/container:go_default_library",
            "@com_github_docker_docker//api/types/image:go_default_library",
            "@com_github_docker_docker//client:go_default_library",
        ],
    )
    for category, binary, leaves, archives in _PAIRED_BENCHMARKS
]
