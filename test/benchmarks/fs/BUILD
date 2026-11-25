load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//test/benchmarks:defs.bzl", "benchmark_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

benchmark_test(
    name = "bazel_test",
    srcs = ["bazel_test.go"],
    use_for_pgo = False,
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/cleanup",
        "//pkg/test/dockerutil",
        "//test/benchmarks/fs/fsbench",
        "//test/benchmarks/harness",
        "//test/benchmarks/tools",
        "@com_github_docker_docker//api/types/mount:go_default_library",
    ],
)

benchmark_test(
    name = "fio_test",
    srcs = ["fio_test.go"],
    use_for_pgo = False,
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/cleanup",
        "//pkg/test/dockerutil",
        "//test/benchmarks/harness",
        "//test/benchmarks/tools",
        "//test/metricsviz",
        "@com_github_docker_docker//api/types/mount:go_default_library",
    ],
)

# The complete Fio family has 48 leaves, three metrics, and five contrasts.
sh_test(
    name = "fio_paired_test",
    srcs = ["//tools/benchmark/paired:test.sh"],
    args = [
        "$(rootpath //test/benchmarks/harness:filesystem_comparison_host_docker)",
        "$(rootpath //tools/benchmark/paired)",
        "$(rootpath :paired_products.json)",
        "-benchstat=$(rootpath @org_golang_x_perf//cmd/benchstat:benchstat)",
        "-cases-file=$(rootpath :fio_cases.json)",
        "-visit-timeout=4m",
        "-iterations=1",
        "-metric-units=sec/op,bandwidth.bytes_per_second,io_ops.ops_per_second",
        "-alpha=0.00001388888888888889",
        "-confidence=0.95",
    ] + [
        "-" + arm + "=" + json.encode([
            "$(rootpath :fio_test)",
            "-test.run=^$",
            "-test.bench=^BenchmarkFio",
            "-test.benchtime=1x",
            "-test.count=1",
            "-test.v",
            "--config_path=/etc/docker/daemon.json",
            "--pgo-benchmarks=false",
            "--runtime=benchmark-" + runtime,
        ])
        for arm, runtime in [("a", "baseline"), ("b", "candidate")]
    ],
    data = [
        "fio_cases.json",
        "paired_products.json",
        ":fio_test",
        "//test/benchmarks/harness:filesystem_comparison_host_docker",
        "//tools/benchmark/paired",
        "@org_golang_x_perf//cmd/benchstat:benchstat",
    ],
    local = True,
    tags = ["manual"],
    target_compatible_with = ["@platforms//cpu:x86_64"],
    timeout = "eternal",
)

benchmark_test(
    name = "rubydev_test",
    srcs = ["rubydev_test.go"],
    use_for_pgo = True,
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/test/dockerutil",
        "//test/benchmarks/fs/fsbench",
        "//test/benchmarks/harness",
        "//test/benchmarks/tools",
    ],
)
