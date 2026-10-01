load("//tools:defs.bzl", "go_test")
load("//tools/bazeldefs:platforms.bzl", "namespace_test_exec_properties")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_test(
    name = "shim_test",
    srcs = [
        "gvisor_task_service_test.go",
        "metric_server_test.go",
        "shim_test.go",
    ],
    deps = [
        "//pkg/shim/v1/taskserver:task_server_go_proto",
        "//shim/shimutils",
        "@com_github_containerd_containerd_api//events:go_default_library",
        "@com_github_containerd_containerd_api//runtime/task/v2:go_default_library",
        "@com_github_containerd_containerd_api//types/task:go_default_library",
        "@com_github_containerd_ttrpc//:go_default_library",
        "@com_github_containerd_typeurl_v2//:go_default_library",
        "@com_github_opencontainers_runtime_spec//specs-go:go_default_library",
        "@org_golang_google_protobuf//types/known/anypb:go_default_library",
        "@org_golang_x_sync//errgroup:go_default_library",
    ],
)

_LIFECYCLE_BENCHMARK_ARGS = [
    "-test.bench=.",
    "-test.run=^$$",
    "-test.benchtime=30x",
    "-test.v",
]

_LIFECYCLE_BENCHMARK_DEPS = [
    "//pkg/cleanup",
    "//pkg/log",
    "//pkg/test/testutil",
    "//runsc/config",
    "//runsc/flag",
    "//runsc/specutils",
    "//shim/shimutils",
    "@com_github_containerd_containerd_api//runtime/task/v2:go_default_library",
    "@com_github_containerd_typeurl_v2//:go_default_library",
    "@com_github_opencontainers_runtime_spec//specs-go:go_default_library",
    "@org_golang_x_sys//unix:go_default_library",
]

go_test(
    name = "lifecycle_benchmark_test",
    size = "large",
    srcs = ["lifecycle_bench_test.go"],
    args = _LIFECYCLE_BENCHMARK_ARGS + ["--test_platforms=kvm,systrap"],
    data = ["//:release"],
    tags = [
        "manual",
        "notap",
        "requires-kvm",
    ],
    deps = _LIFECYCLE_BENCHMARK_DEPS,
)

# Run the public shim workloads with each platform independently selectable.
[
    go_test(
        name = "lifecycle_benchmark_" + platform + "_test",
        size = "large",
        timeout = "eternal",
        srcs = ["lifecycle_bench_test.go"],
        args = _LIFECYCLE_BENCHMARK_ARGS + ["--test_platforms=" + platform],
        data = ["//:release"],
        exec_properties = namespace_test_exec_properties(memory = "8GB"),
        nogo = False,
        tags = [
            "continuous-benchmark",
            "manual",
        ] + (["requires-kvm"] if platform == "kvm" else []),
        visibility = ["//test/benchmarks:__pkg__"],
        deps = _LIFECYCLE_BENCHMARK_DEPS,
    )
    for platform in [
        "kvm",
        "systrap",
    ]
]
