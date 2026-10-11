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
    continuous = [
        {
            "name": "",
            "benchtime": "30s",
            "cohort": "benchmark_iperf",
            "filter": "BenchmarkIperfOneConnection",
        },
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
    continuous = [
        {
            "name": "",
            "benchtime": "30s",
            "cohort": "benchmark_node",
            "filter": ".",
        },
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
    name = "ruby_test",
    srcs = [
        "ruby_test.go",
    ],
    continuous = [
        {
            "name": "",
            "benchtime": "30s",
            "cohort": "benchmark_ruby",
            "filter": ".",
        },
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
    continuous = [
        {
            "name": "",
            "benchtime": "30s",
            "cohort": "benchmark_nginx",
            "filter": "Continuous",
        },
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
    continuous = [
        {
            "name": "",
            "benchtime": "30s",
            "cohort": "benchmark_httpd",
            "filter": "Continuous",
        },
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
