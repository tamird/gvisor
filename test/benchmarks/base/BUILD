load("@rules_python//python:py_test.bzl", "py_test")
load("//test/benchmarks:defs.bzl", "benchmark_test")
load("//test/docker:config.bzl", "STARTUP_VARIANTS")
load("//tools:defs.bzl", "go_library")
load("//tools/bazeldefs:platforms.bzl", "docker_test_exec_properties")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "base",
    testonly = 1,
    srcs = [
        "base.go",
    ],
    visibility = ["//tools/codeql:__pkg__"],
    deps = [
        "//pkg/test/dockerutil",
        "//test/benchmarks/harness",
    ],
)

benchmark_test(
    name = "startup_test",
    srcs = ["startup_test.go"],
    cohort = "startup",
    continuous = [
        {
            "name": "",
            "benchtime": "30s",
            "cohort": "benchmark_servers",
            "filter": ".",
        },
    ],
    owned_args = [
        "-test.v",
        "-test.bench=BenchmarkStartupEmpty",
        "-test.benchtime=1ns",
    ],
    runtime_variants = STARTUP_VARIANTS,
    use_for_pgo = False,
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/test/dockerutil",
        "//pkg/test/testutil",
        "//test/benchmarks/base",
        "//test/benchmarks/harness",
        "//test/metricsviz",
    ],
)

benchmark_test(
    name = "size_test",
    srcs = ["size_test.go"],
    use_for_pgo = False,
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/test/dockerutil",
        "//test/benchmarks/base",
        "//test/benchmarks/harness",
        "//test/benchmarks/tools",
        "//test/metricsviz",
    ],
)

benchmark_test(
    name = "sysbench_test",
    srcs = ["sysbench_test.go"],
    continuous = [
        {
            "name": "",
            "benchtime": "30s",
            "cohort": "benchmark_sysbench",
            "filter": ".",
        },
    ],
    use_for_pgo = True,
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/test/dockerutil",
        "//test/benchmarks/harness",
        "//test/benchmarks/tools",
        "//test/metricsviz",
    ],
)

benchmark_test(
    name = "syscallbench_test",
    srcs = ["syscallbench_test.go"],
    continuous = [
        {
            "name": "",
            "benchtime": "30s",
            "cohort": "benchmark_syscallbench",
            "filter": ".",
        },
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
    name = "hackbench_test",
    srcs = ["hackbench_test.go"],
    continuous = [
        {
            "name": "",
            "benchtime": "30s",
            "cohort": "benchmark_hackbench",
            "filter": ".",
        },
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
    name = "usage_test",
    srcs = ["usage_test.go"],
    continuous = [
        {
            "name": "",
            "benchtime": "30s",
            "cohort": "benchmark_servers",
            "filter": ".",
        },
    ],
    use_for_pgo = False,
    visibility = ["//:sandbox"],
    deps = [
        ":base",
        "//pkg/test/dockerutil",
        "//test/benchmarks/harness",
        "//test/benchmarks/tools",
        "//test/metricsviz",
        "@com_github_docker_docker//api/types/container:go_default_library",
    ],
)

# Diagnostic wrapper preserves the existing StartupEmpty implementation and
# declared private Docker fixture while choosing each source-bound release.
py_test(
    name = "state_startup_pair_test",
    size = "large",
    timeout = "long",
    srcs = ["//test/rbe/state_full_startup:sandbox.py"],
    args = [
        "$(rootpath @org_golang_x_perf//cmd/benchstat)",
        "$(rootpath :startup_test_systrap_owned)",
        "--docker_test_config=$(rootpath :startup_test_systrap_docker_config)",
        "-test.v=false",
        "-test.run=^$$",
        "-test.bench=^BenchmarkStartupEmpty$$",
        "-test.benchtime=1x",
        "-test.count=8",
    ],
    data = [
        ":startup_test_systrap_docker_config",
        ":startup_test_systrap_owned",
        "//test/rbe/state_full_startup:releases",
        "@org_golang_x_perf//cmd/benchstat",
    ],
    exec_properties = docker_test_exec_properties(
        free_disk = "40GB",
        memory = "8GB",
    ),
    main = "//test/rbe/state_full_startup:sandbox.py",
    tags = ["manual"],
)
