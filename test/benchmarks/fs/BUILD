load("//test/benchmarks:defs.bzl", "benchmark_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

benchmark_test(
    name = "bazel_test",
    srcs = ["bazel_test.go"],
    continuous = [
        {
            "name": "absl",
            "benchtime": "1ns",
            "cohort": "benchmark_absl",
            "filter": "ABSL/page_cache.clean",
        },
        {
            "name": "grpc",
            "benchtime": "1ns",
            "cohort": "benchmark_grpc",
            "filter": "GRPC/page_cache.clean/filesystem.bind",
        },
    ],
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
    continuous = [
        {
            "name": "write",
            "benchtime": "1000x",
            "cohort": "benchmark_fio",
            "filter": "BenchmarkFioWrite",
        },
        {
            "name": "read",
            "benchtime": "1000x",
            "cohort": "benchmark_fio",
            "filter": "BenchmarkFioRead",
        },
        {
            "name": "randwrite",
            "benchtime": "1000x",
            "cohort": "benchmark_fio",
            "filter": "BenchmarkFioRandWrite",
        },
        {
            "name": "randread",
            "benchtime": "1000x",
            "cohort": "benchmark_fio",
            "filter": "BenchmarkFioRandRead",
        },
    ],
    data = ["//test/runner/fuse"],
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

benchmark_test(
    name = "rubydev_test",
    srcs = ["rubydev_test.go"],
    continuous = [
        {
            "name": "",
            "benchtime": "1ns",
            "cohort": "benchmark_rubydev",
            "filter": ".",
        },
    ],
    use_for_pgo = True,
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/test/dockerutil",
        "//test/benchmarks/fs/fsbench",
        "//test/benchmarks/harness",
        "//test/benchmarks/tools",
    ],
)
