load("//tools:defs.bzl", "build_test", "go_library", "go_test")

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

build_test(
    name = "paired_runtime_products_test",
    tags = ["manual"],
    targets = [
        "//test/benchmarks/network:node_test",
        "@benchmark_runtime_base//:release",
        "@benchmark_runtime_candidate//:release",
    ],
)

filegroup(
    name = "paired_base_runsc",
    srcs = ["@benchmark_runtime_base//:release"],
    output_group = "runsc",
)

filegroup(
    name = "paired_candidate_runsc",
    srcs = ["@benchmark_runtime_candidate//:release"],
    output_group = "runsc",
)

go_test(
    name = "paired_worker_test",
    size = "large",
    timeout = "long",
    srcs = [
        "docker_worker_test.go",
        "paired_observer_test.go",
        "paired_worker_test.go",
    ],
    args = [
        "--alpine-archive=$(rootpath //images/basic:alpine_archive)",
        "--paired-base-runsc=$(rootpath :paired_base_runsc)",
        "--paired-candidate-runsc=$(rootpath :paired_base_runsc)",
        "--paired-base-source=a05ab5909d2518dd541a1eb4396057e9292f4178",
        "--paired-candidate-source=a05ab5909d2518dd541a1eb4396057e9292f4178",
        "--paired-benchmark=$(rootpath //test/benchmarks/network:node_test)",
        "--paired-bench=^BenchmarkNode$$/^concurrency.1$$",
        "--paired-benchtime=5000x",
        "--paired-leaves=1",
        "--paired-observe-affinity=true",
        "--paired-node-warmup-requests=5000",
        "--paired-image-archives='$(rootpaths //test/benchmarks/network:node_worker_images)'",
        "-test.run=^TestPairedBenchmark$$",
    ],
    data = [
        ":paired_base_runsc",
        ":paired_candidate_runsc",
        "//images/basic:alpine_archive",
        "//test/benchmarks/network:node_test",
        "//test/benchmarks/network:node_worker_images",
        "@benchmark_runtime_base//:release",
        "@benchmark_runtime_candidate//:release",
    ],
    embed = [":harness"],
    exec_properties = {
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
        "@com_github_docker_docker//api/types/events:go_default_library",
        "@com_github_docker_docker//api/types/filters:go_default_library",
        "@com_github_docker_docker//api/types/image:go_default_library",
        "@com_github_docker_docker//api/types/mount:go_default_library",
        "@com_github_docker_docker//api/types/network:go_default_library",
        "@com_github_docker_docker//client:go_default_library",
        "@com_github_docker_docker//errdefs:go_default_library",
    ],
)
