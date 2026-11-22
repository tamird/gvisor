load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
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

filegroup(
    name = "coverage_runsc_entrypoint",
    testonly = True,
    srcs = ["//test/runner:coverage_release"],
    output_group = "runsc",
)

filegroup(
    name = "legacy_coverage_runsc_entrypoint",
    testonly = True,
    srcs = ["//test/runner:legacy_coverage_release"],
    output_group = "runsc",
)

go_test(
    name = "coverage_worker_test",
    size = "large",
    timeout = "long",
    srcs = ["docker_worker_test.go"],
    args = [
        "--alpine-archive=$(rootpath //images/basic:alpine_archive)",
        "--worker-runtime=runsc",
        "--release-runsc=$(rootpath :coverage_runsc_entrypoint)",
    ],
    data = [
        ":coverage_runsc_entrypoint",
        "//test/runner:coverage_release",
        "//images/basic:alpine_archive",
    ],
    embed = [":harness"],
    exec_properties = {
        "test.EstimatedCPU": "4",
        "test.EstimatedMemory": "500M",
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
        "@com_github_docker_docker//api/types/container:go_default_library",
        "@com_github_docker_docker//api/types/mount:go_default_library",
        "@com_github_docker_docker//api/types/network:go_default_library",
        "@com_github_docker_docker//client:go_default_library",
        "@com_github_docker_docker//errdefs:go_default_library",
    ],
)
go_test(
    name = "legacy_coverage_worker_test",
    size = "large",
    timeout = "long",
    srcs = ["docker_worker_test.go"],
    args = [
        "--alpine-archive=$(rootpath //images/basic:alpine_archive)",
        "--worker-runtime=runsc",
        "--release-runsc=$(rootpath :legacy_coverage_runsc_entrypoint)",
    ],
    data = [
        ":legacy_coverage_runsc_entrypoint",
        "//test/runner:legacy_coverage_release",
        "//images/basic:alpine_archive",
    ],
    embed = [":harness"],
    exec_properties = {
        "test.EstimatedCPU": "4",
        "test.EstimatedMemory": "500M",
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
        "@com_github_docker_docker//api/types/container:go_default_library",
        "@com_github_docker_docker//api/types/mount:go_default_library",
        "@com_github_docker_docker//api/types/network:go_default_library",
        "@com_github_docker_docker//client:go_default_library",
        "@com_github_docker_docker//errdefs:go_default_library",
    ],
)

_COVERAGE_INITIALIZATIONS = [
    ("startup", "//test/benchmarks/base:startup_test", 1, "//images:benchmark_alpine_archive", []),
    ("syscall", "//test/benchmarks/base:syscallbench_test", 7, "//images/benchmarks/syscallbench:syscallbench_archive", ["@platforms//cpu:x86_64"]),
    ("hackbench", "//test/benchmarks/base:hackbench_test", 2, "//images:benchmark_hackbench_archive", ["@platforms//cpu:aarch64"]),
]

[
    go_test(
        name = "coverage_" + category + "_initialization_test",
        size = "large",
        timeout = "long",
        srcs = [
            "docker_worker_test.go",
            "paired_products_test.go",
            "paired_worker_test.go",
        ],
        args = [
            "--paired-base-runsc=$(rootpath :coverage_runsc_entrypoint)",
            "--paired-candidate-runsc=$(rootpath :coverage_runsc_entrypoint)",
            "--paired-base-source=57c5e7b58eb7d0657aa0091065e7c2b16aeab735",
            "--paired-candidate-source=57c5e7b58eb7d0657aa0091065e7c2b16aeab735",
            "--paired-benchmark=$(rootpath %s)" % binary,
            "--paired-leaves=%d" % leaves,
            "--paired-image-archives=$(rootpath %s)" % archive,
            "--paired-benchtime=1x",
            "--paired-initialize-only",
            "--client-cpuset=0-1",
            "--server-cpuset=2-3",
            "-test.run=^TestPairedBenchmark$$",
        ],
        data = [
            ":coverage_runsc_entrypoint",
            "//test/runner:coverage_release",
            binary,
            archive,
        ],
        embed = [":harness"],
        exec_properties = {
            "test.EstimatedCPU": "4",
            "test.EstimatedMemory": "500M",
            "test.init-dockerd": "false",
            "test.workload-isolation-type": "firecracker",
        },
        pure = True,
        tags = ["manual"],
        target_compatible_with = ["@platforms//os:linux"] + constraints,
        deps = [
            "//pkg/test/testutil",
            "@com_github_docker_docker//api/types/container:go_default_library",
            "@com_github_docker_docker//api/types/image:go_default_library",
            "@com_github_docker_docker//api/types/mount:go_default_library",
            "@com_github_docker_docker//api/types/network:go_default_library",
            "@com_github_docker_docker//client:go_default_library",
            "@com_github_docker_docker//errdefs:go_default_library",
            "@org_golang_x_sys//unix:go_default_library",
        ],
    )
    for category, binary, leaves, archive, constraints in _COVERAGE_INITIALIZATIONS
]
