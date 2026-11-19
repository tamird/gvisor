load("//test/benchmarks:defs.bzl", "benchmark_test")
load("//tools:defs.bzl", "go_library")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

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
    ],
    args = [
        "--owned-docker",
        "--worker-image-archives=$(rootpath //images:benchmark_node_archive),$(rootpath //images:benchmark_hey_archive),$(rootpath //images:benchmark_util_archive),$(rootpath //images/basic:benchmark_redis_archive)",
        "-test.run=^$$",
        "-test.bench=^BenchmarkNode$$",
        "-test.benchtime=25x",
    ],
    data = [
        "//images:benchmark_hey_archive",
        "//images:benchmark_node_archive",
        "//images:benchmark_util_archive",
        "//images/basic:benchmark_redis_archive",
    ],
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
    name = "node_runsc_worker_test",
    srcs = [
        "node_test.go",
    ],
    args = [
        "--owned-docker",
        "--worker-runsc=$(rootpath //test/benchmarks/harness:runsc_entrypoint)",
        "--worker-image-archives=$(rootpath //images:benchmark_node_archive),$(rootpath //images:benchmark_hey_archive),$(rootpath //images:benchmark_util_archive),$(rootpath //images/basic:benchmark_redis_archive)",
        "-test.run=^$$",
        "-test.bench=^BenchmarkNode$$",
        "-test.benchtime=25x",
    ],
    data = [
        "//:release",
        "//test/benchmarks/harness:runsc_entrypoint",
        "//images:benchmark_hey_archive",
        "//images:benchmark_node_archive",
        "//images:benchmark_util_archive",
        "//images/basic:benchmark_redis_archive",
    ],
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
