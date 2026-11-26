load("@bazel_skylib//lib:shell.bzl", "shell")
load("@rules_shell//shell:sh_test.bzl", "sh_test")
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

sh_test(
    name = "node_control_test",
    timeout = "eternal",
    srcs = ["//tools/benchmark/paired:test.sh"],
    args = [
        "--network",
        "$(rootpath //test/benchmarks/harness:network_comparison_host_docker)",
        "$(rootpath //tools/benchmark/paired)",
        "$(rootpath //tools/benchmark/paired:products.json)",
        "-benchstat=$(rootpath @org_golang_x_perf//cmd/benchstat:benchstat)",
        shell.quote("-cases=" + json.encode({
            "//test/benchmarks/network:node_control_test": [
                "Node/concurrency." + str(concurrency)
                for concurrency in [
                    1,
                    5,
                    10,
                    25,
                ]
            ],
        })),
        "-control-only",
        "-visit-timeout=9m",
        "-iterations=1000",
        "-metric-units=sec/op,requests_per_second.QPS,average_latency.s",
        "-alpha=3.690036900369004e-06",
        "-confidence=0.95",
    ] + [
        shell.quote("-" + arm + "=" + json.encode([
            "$(rootpath :node_test)",
            "-test.run=^$$",
            "-test.bench=^BenchmarkNode$$",
            "-test.benchtime=1000x",
            "-test.count=1",
            "-test.v",
            "--config_path=/etc/docker/daemon.json",
            "--pgo-benchmarks=false",
            "--runtime=benchmark-candidate",
        ]))
        for arm in [
            "a",
            "b",
        ]
    ],
    data = [
        ":node_test",
        "//test/benchmarks/harness:network_comparison_host_docker",
        "//tools/benchmark/paired",
        "//tools/benchmark/paired:products.json",
        "@org_golang_x_perf//cmd/benchstat",
    ],
    local = True,
    tags = ["manual"],
    target_compatible_with = ["@platforms//cpu:x86_64"],
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
