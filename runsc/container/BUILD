load("//tools:defs.bzl", "default_platform", "go_library", "go_test", "most_shards", "native_test", "platforms")
load("//tools/bazeldefs:platforms.bzl", "namespace_test_exec_properties")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "container",
    srcs = [
        "container.go",
        "gofer_to_host_rpc.go",
        "null_netns.go",
        "state_file.go",
        "status.go",
    ],
    visibility = [
        "//runsc:__subpackages__",
        "//test:__subpackages__",
    ],
    deps = [
        "//pkg/abi/linux",
        "//pkg/cleanup",
        "//pkg/log",
        "//pkg/sentry/control",
        "//pkg/sentry/fsimpl/erofs",
        "//pkg/sentry/fsimpl/tmpfs",
        "//pkg/sentry/pgalloc",
        "//pkg/sentry/state/checkpointfiles",
        "//pkg/sighandling",
        "//pkg/sync",
        "//pkg/unet",
        "//pkg/urpc",
        "//runsc/boot",
        "//runsc/cgroup",
        "//runsc/config",
        "//runsc/console",
        "//runsc/donation",
        "//runsc/profile",
        "//runsc/sandbox",
        "//runsc/specutils",
        "//runsc/starttime",
        "@com_github_gofrs_flock//:go_default_library",
        "@com_github_opencontainers_runtime_spec//specs-go:go_default_library",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

# configs() in container_test.go already excludes ptrace. Keep the remaining
# public platforms selectable without rebuilding or narrowing the test binary.
_CONTAINER_PLATFORMS = [p for (p, tags) in platforms.items() if "manual" not in tags and p != "ptrace"]

go_test(
    name = "container_test",
    size = "large",
    srcs = [
        "console_test.go",
        "container_save_restore_test.go",
        "container_test.go",
        "metric_server_test.go",
        "multi_container_save_restore_test.go",
        "multi_container_test.go",
        "shared_volume_test.go",
        "trace_test.go",
    ],
    # Only run the default platform for the tsan test, which should
    # be compatible. For non-tsan builds, run all platforms.
    args = select({
        "//conditions:default": [
            "--test_platforms=%s" % ",".join([p for (p, tags) in platforms.items() if "manual" not in tags]) or default_platform,
        ],
        "//tools:gotsan": [
            "--test_platforms=%s" % default_platform,
        ],
    }),
    cgroup_v2 = True,
    data = [
        "//:release",
        "//test/cmd/test_app",
    ],
    exec_properties = namespace_test_exec_properties(),
    library = ":container",
    shard_count = most_shards,
    tags = ["requires-kvm"] + ["rbe-has-container-%s-variant" % p for p in _CONTAINER_PLATFORMS],
    deps = [
        "//pkg/abi/linux",
        "//pkg/cleanup",
        "//pkg/hostos",
        "//pkg/log",
        "//pkg/seccheck/scsdk",
        "//pkg/sentry/checkpoint",
        "//pkg/sentry/control",
        "//pkg/sentry/fscheckpoint",
        "//pkg/sentry/fscheckpoint:fscheckpoint_proto_go_proto",
        "//pkg/sentry/fsimpl/erofs",
        "//pkg/sentry/kernel",
        "//pkg/sentry/kernel/auth",
        "//pkg/sentry/limits",
        "//pkg/sentry/platform",
        "//pkg/sentry/seccheck",
        "//pkg/sentry/seccheck/points:points_go_proto",
        "//pkg/sentry/seccheck/sinks/remote/test",
        "//pkg/sentry/state/checkpointfiles",
        "//pkg/state/statefile",
        "//pkg/sync",
        "//pkg/test/testutil",
        "//pkg/unet",
        "//runsc/boot",
        "//runsc/cgroup",
        "//runsc/config",
        "//runsc/flag",
        "//runsc/sandbox",
        "//runsc/specutils",
        "//test/metricclient",
        "@com_github_cenkalti_backoff//:go_default_library",
        "@com_github_creack_pty//:go_default_library",
        "@com_github_google_go_cmp//cmp:go_default_library",
        "@com_github_opencontainers_runtime_spec//specs-go:go_default_library",
        "@org_golang_google_grpc//:go_default_library",
        "@org_golang_google_protobuf//proto:go_default_library",
        "@org_golang_google_protobuf//types/known/emptypb:go_default_library",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

[
    native_test(
        name = "container_test_" + platform,
        size = "large",
        src = ":container_test",
        args = ["--test_platforms=" + platform],
        cgroup_v2 = True,
        # Preserve rules_go's runfiles initialization in the shared executable.
        env = {"GO_TEST_RUN_FROM_BAZEL": "1"},
        exec_properties = namespace_test_exec_properties(),
        shard_count = most_shards,
        tags = ["manual"] + (["requires-kvm"] if platform == "kvm" else []),
    )
    for platform in _CONTAINER_PLATFORMS
]

go_test(
    name = "serialization_test",
    srcs = ["serialization_test.go"],
    deps = [
        ":container",
        "//runsc/sandbox",
        "@org_golang_google_protobuf//proto:go_default_library",
    ],
)

_LIFECYCLE_BENCHMARK_DATA = [
    "//:release",
    "//test/cmd/test_app",
]

_LIFECYCLE_BENCHMARK_DEPS = [
    "//pkg/log",
    "//pkg/test/testutil",
    "//runsc/config",
    "//runsc/flag",
    "//runsc/specutils",
    "@org_golang_x_sys//unix:go_default_library",
]

_LIFECYCLE_BENCHMARK_ARGS = [
    "-test.bench=.",
    "-test.run=^$$",
    "-test.benchtime=30x",  # A fixed iteration count.
    "-test.v",
]

go_test(
    name = "lifecycle_benchmark_test",
    size = "large",
    srcs = ["lifecycle_bench_test.go"],
    args = _LIFECYCLE_BENCHMARK_ARGS + ["--test_platforms=kvm,systrap"],
    data = _LIFECYCLE_BENCHMARK_DATA,
    library = ":container",
    tags = [
        "manual",
        "notap",
        "requires-kvm",
    ],
    deps = _LIFECYCLE_BENCHMARK_DEPS,
)

# Use the same direct runtime owner, separately selectable for hosted systrap
# and KVM-capable workers. No Docker daemon or runc variant is involved.
[
    go_test(
        name = "lifecycle_benchmark_" + platform + "_test",
        size = "large",
        timeout = "eternal",
        srcs = ["lifecycle_bench_test.go"],
        args = _LIFECYCLE_BENCHMARK_ARGS + ["--test_platforms=" + platform],
        data = _LIFECYCLE_BENCHMARK_DATA,
        exec_properties = namespace_test_exec_properties(memory = "8GB"),
        library = ":container",
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
