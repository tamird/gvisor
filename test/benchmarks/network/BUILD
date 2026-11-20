load("//test/benchmarks:defs.bzl", "benchmark_test")
load("//tools:defs.bzl", "go_library")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

_NODE_WORKER_IMAGES = [
    "//images:benchmark_hey_read_checked_archive",
    "//images:benchmark_node_completed_archive",
    "//images:benchmark_util_archive",
    "//images/basic:benchmark_redis_archive",
]

go_library(
    name = "network",
    testonly = 1,
    srcs = [
        "network.go",
    ],
    deps = [
        "//pkg/test/dockerutil",
        "//test/benchmarks/harness",
        "//test/benchmarks/tools",
        "//test/metricsviz",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

benchmark_test(
    name = "iperf_test",
    srcs = [
        "iperf_test.go",
    ],
    embed = [":network"],
    use_for_pgo = True,
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/test/dockerutil",
        "//pkg/test/testutil",
        "//test/benchmarks/harness",
        "//test/benchmarks/tools",
        "//test/metricsviz",
    ],
)

benchmark_test(
    name = "node_test",
    srcs = [
        "node_test.go",
    ],
    data = [
        "//images:benchmarks/node/index.js",
        "//images:benchmarks/node/index_test.js",
    ],
    embed = [":network"],
    use_for_pgo = False,
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/test/dockerutil",
        "//test/benchmarks/harness",
        "//test/benchmarks/tools",
        "//test/metricsviz",
    ],
)

benchmark_test(
    name = "node_worker_test",
    srcs = [
        "node_test.go",
        "node_worker_test.go",
    ],
    args = [
        "--owned-docker",
        "--worker-bridge=false",
        "--worker-image-archives=$(rootpath //images:benchmark_hey_read_checked_archive)",
        "-test.v=test2json",
        "-test.run=^TestHeyReportInterface$$",
        "-test.bench=^$$",
    ],
    data = [
        "//images:benchmark_hey_read_checked_archive",
        "//images:benchmarks/node/index.js",
        "//images:benchmarks/node/index_test.js",
    ],
    embed = [":network"],
    exec_properties = {
        "test.EstimatedCPU": "5",
        "test.EstimatedMemory": "500M",
        "test.init-dockerd": "false",
        "test.workload-isolation-type": "firecracker",
    },
    local = False,
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    use_for_pgo = False,
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/test/dockerutil",
        "//test/benchmarks/harness",
        "//test/benchmarks/tools",
        "//test/metricsviz",
    ],
)

benchmark_test(
    name = "node_runsc_worker_test",
    srcs = [
        "node_test.go",
    ],
    args = [
        "--owned-docker",
        "--worker-runsc=$(rootpath //test/benchmarks/harness:runsc_entrypoint)",
        "--worker-image-archives=" + ",".join(["$(rootpath %s)" % image for image in _NODE_WORKER_IMAGES]),
        "-test.run=^$$",
        "-test.bench=^BenchmarkNode$$",
        "-test.benchtime=25x",
    ],
    data = [
        "//:release",
        "//images:benchmarks/node/index.js",
        "//images:benchmarks/node/index_test.js",
        "//test/benchmarks/harness:runsc_entrypoint",
    ] + _NODE_WORKER_IMAGES,
    embed = [":network"],
    exec_properties = {
        "test.init-dockerd": "false",
        "test.workload-isolation-type": "firecracker",
    },
    local = False,
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    use_for_pgo = False,
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/test/dockerutil",
        "//test/benchmarks/harness",
        "//test/benchmarks/tools",
        "//test/metricsviz",
    ],
)

benchmark_test(
    name = "ruby_test",
    srcs = [
        "ruby_test.go",
    ],
    embed = [":network"],
    use_for_pgo = False,
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/test/dockerutil",
        "//test/benchmarks/harness",
        "//test/benchmarks/tools",
        "//test/metricsviz",
    ],
)

benchmark_test(
    name = "nginx_test",
    srcs = [
        "nginx_test.go",
    ],
    embed = [":network"],
    use_for_pgo = True,
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/test/dockerutil",
        "//test/benchmarks/harness",
        "//test/benchmarks/tools",
    ],
)

benchmark_test(
    name = "httpd_test",
    srcs = [
        "httpd_test.go",
    ],
    embed = [":network"],
    use_for_pgo = False,
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/test/dockerutil",
        "//pkg/test/testutil",
        "//test/benchmarks/harness",
        "//test/benchmarks/tools",
    ],
)

filegroup(
    name = "node_worker_images",
    srcs = _NODE_WORKER_IMAGES,
    visibility = ["//test/benchmarks/harness:__pkg__"],
)
