load("@bazel_skylib//lib:shell.bzl", "shell")
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

sh_test(
    name = "grpc_dirty_bindfs_control_test",
    timeout = "eternal",
    srcs = ["//tools/benchmark/paired:test.sh"],
    args = [
        "$(rootpath //test/benchmarks/harness:filesystem_comparison_host_docker)",
        "$(rootpath //tools/benchmark/paired)",
        "$(rootpath :paired_products.json)",
        "-benchstat=$(rootpath @org_golang_x_perf//cmd/benchstat:benchstat)",
        shell.quote("-cases=" + json.encode({
            "//test/benchmarks/fs:grpc_dirty_bindfs_control_test": [
                "BuildGRPC/page_cache.dirty/filesystem.bindfs",
            ],
        })),
        "-control-only",
        "-visit-timeout=10m",
        "-iterations=1",
        "-metric-units=sec/op",
        "-alpha=0.00025",
        "-confidence=0.95",
    ] + [
        shell.quote("-" + arm + "=" + json.encode([
            "$(rootpath :bazel_test)",
            "-test.run=^$$",
            "-test.bench=^BenchmarkBuildGRPC$$/^page_cache[.]dirty$$/^filesystem[.]bindfs$$",
            "-test.benchtime=1x",
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
        "paired_products.json",
        ":bazel_test",
        "//test/benchmarks/harness:filesystem_comparison_host_docker",
        "//tools/benchmark/paired",
        "@org_golang_x_perf//cmd/benchstat",
    ],
    local = True,
    tags = ["manual"],
    target_compatible_with = ["@platforms//cpu:x86_64"],
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

# Retain the eight-leaf Abseil family's alpha .01 / 40 contrasts.
sh_test(
    name = "absl_dirty_fuse_paired_test",
    timeout = "eternal",
    srcs = ["absl_paired.sh"],
    args = [
        "$(rootpath //test/benchmarks/harness:filesystem_comparison_host_docker)",
        "$(rootpath //tools/benchmark/paired)",
        "$(rootpath :paired_products.json)",
        "$(rootpath //images:benchmarks/absl/Dockerfile.x86_64)",
        "-benchstat=$(rootpath @org_golang_x_perf//cmd/benchstat:benchstat)",
        shell.quote("-cases=" + json.encode({
            "//test/benchmarks/fs:absl_dirty_fuse_paired_test": [
                "BuildABSL/page_cache.dirty/filesystem.fusefs",
            ],
        })),
        "-visit-timeout=4m",
        "-iterations=1",
        "-metric-units=sec/op",
        "-alpha=0.00025",
        "-confidence=0.95",
    ] + [
        shell.quote("-" + arm + "=" + json.encode([
            "$(rootpath :bazel_test)",
            "-test.run=^$$",
            "-test.bench=^BenchmarkBuildABSL$$/^page_cache[.]dirty$$/^filesystem[.]fusefs$$",
            "-test.benchtime=1x",
            "-test.count=1",
            "-test.v",
            "--config_path=/etc/docker/daemon.json",
            "--pgo-benchmarks=false",
            "--runtime=benchmark-" + runtime,
        ]))
        for arm, runtime in [
            ("a", "baseline"),
            ("b", "candidate"),
        ]
    ],
    data = [
        "paired_products.json",
        ":bazel_test",
        "//images:benchmarks/absl/Dockerfile.x86_64",
        "//test/benchmarks/harness:filesystem_comparison_host_docker",
        "//tools/benchmark/paired",
        "@org_golang_x_perf//cmd/benchstat",
    ],
    local = True,
    tags = ["manual"],
    target_compatible_with = ["@platforms//cpu:x86_64"],
)

# The complete Fio family has 48 leaves, three metrics, and five contrasts.
sh_test(
    name = "fio_paired_test",
    timeout = "eternal",
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
        shell.quote("-" + arm + "=" + json.encode([
            "$(rootpath :fio_test)",
            "-test.run=^$$",
            "-test.bench=^BenchmarkFio",
            "-test.benchtime=1x",
            "-test.count=1",
            "-test.v",
            "--config_path=/etc/docker/daemon.json",
            "--pgo-benchmarks=false",
            "--runtime=benchmark-" + runtime,
        ]))
        for arm, runtime in [
            ("a", "baseline"),
            ("b", "candidate"),
        ]
    ],
    data = [
        "fio_cases.json",
        "paired_products.json",
        ":fio_test",
        "//test/benchmarks/harness:filesystem_comparison_host_docker",
        "//tools/benchmark/paired",
        "@org_golang_x_perf//cmd/benchstat",
    ],
    local = True,
    tags = ["manual"],
    target_compatible_with = ["@platforms//cpu:x86_64"],
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

# Keep the complete eight-leaf RubySpec family at alpha .01 / 80 contrasts.
[
    sh_test(
        name = "rubyspec_" + filesystem + "_paired_test",
        timeout = "eternal",
        srcs = ["//tools/benchmark/paired:test.sh"],
        args = [
            "$(rootpath //test/benchmarks/harness:filesystem_comparison_host_docker)",
            "$(rootpath //tools/benchmark/paired)",
            "$(rootpath :paired_products.json)",
            "-benchstat=$(rootpath @org_golang_x_perf//cmd/benchstat:benchstat)",
            shell.quote("-cases=" + json.encode({
                "//test/benchmarks/fs:rubyspec_" + filesystem + "_paired_test": [
                    "RubySpecTest/page_cache." + cache + "/filesystem." + filesystem
                    for cache in [
                        "clean",
                        "dirty",
                    ]
                ],
            })),
            "-visit-timeout=4m",
            "-iterations=1",
            "-metric-units=sec/op,load.ns",
            "-alpha=0.000125",
            "-confidence=0.95",
        ] + [
            shell.quote("-" + arm + "=" + json.encode([
                "$(rootpath :rubydev_test)",
                "-test.run=^$$",
                "-test.bench=^BenchmarkRubySpecTest$$/^page_cache[.](clean|dirty)$$/^filesystem[.]" + filesystem + "$$",
                "-test.benchtime=1x",
                "-test.count=1",
                "-test.v",
                "--config_path=/etc/docker/daemon.json",
                "--pgo-benchmarks=false",
                "--runtime=benchmark-" + runtime,
            ]))
            for arm, runtime in [
                ("a", "baseline"),
                ("b", "candidate"),
            ]
        ],
        data = [
            "paired_products.json",
            ":rubydev_test",
            "//test/benchmarks/harness:filesystem_comparison_host_docker",
            "//tools/benchmark/paired",
            "@org_golang_x_perf//cmd/benchstat",
        ],
        local = True,
        tags = ["manual"],
        target_compatible_with = ["@platforms//cpu:x86_64"],
    )
    for filesystem in [
        "bindfs",
        "tmpfs",
        "rootfs",
        "fusefs",
    ]
]
