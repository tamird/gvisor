load("//test/rbe/tcg:defs.bzl", "amd64_kvm_test")
load("//tools:defs.bzl", "default_platform", "go_library", "go_test", "most_shards", "native_test", "platforms")
load("//tools/bazeldefs:platforms.bzl", "RBE_NETWORK_TOOLS_IMAGE", "namespace_test_exec_properties")
load(":test_tools.bzl", "erofs_tools")

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
        "//pkg/sentry/control/controlapi",
        "//pkg/sentry/hostmm",
        "//pkg/sighandling",
        "//pkg/sync",
        "//pkg/unet",
        "//pkg/urpc",
        "//runsc/boot/sentryapi",
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

_CONTAINER_PLATFORM_ARGS = {platform: ["--test_platforms=" + platform] for platform in _CONTAINER_PLATFORMS}

# The tests execute ip from the container root and create memory-backed files
# in up to 25 concurrent containers, each writing 100 MiB.
_CONTAINER_EXEC_PROPERTIES = namespace_test_exec_properties(
    image = RBE_NETWORK_TOOLS_IMAGE,
    memory = "4GB",
)

erofs_tools(
    name = "erofs_tools",
    testonly = True,
    src = "@container_test_tools//:flat",
)

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
        ":erofs_tools",
        "//:release",
        "//test/cmd/test_app",
    ],
    embed = [":container"],
    exec_properties = _CONTAINER_EXEC_PROPERTIES,
    shard_count = most_shards,
    tags = ["requires-kvm"] + ["rbe-has-container-%s-variant" % p for p in _CONTAINER_PLATFORMS],
    deps = [
        "//pkg/abi/linux",
        "//pkg/cleanup",
        "//pkg/hostos",
        "//pkg/log",
        "//pkg/seccheck/scsdk",
        "//pkg/sentry/control/controlapi",
        "//pkg/sentry/fscheckpoint",
        "//pkg/sentry/fscheckpoint:fscheckpoint_proto_go_proto",
        "//pkg/sentry/fsimpl/erofs",
        "//pkg/sentry/kernel/auth",
        "//pkg/sentry/limits",
        "//pkg/sentry/platform/platformdesc",
        "//pkg/sentry/seccheck",
        "//pkg/sentry/seccheck/points:points_go_proto",
        "//pkg/sentry/seccheck/sinks/remote/test",
        "//pkg/sentry/state/checkpointfiles",
        "//pkg/state/statefile",
        "//pkg/sync",
        "//pkg/test/testutil",
        "//pkg/unet",
        "//runsc/boot/procfs",
        "//runsc/boot/sentryapi",
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
        args = _CONTAINER_PLATFORM_ARGS[platform],
        cgroup_v2 = True,
        # Preserve rules_go's runfiles initialization in the shared executable.
        env = {"GO_TEST_RUN_FROM_BAZEL": "1"},
        exec_properties = _CONTAINER_EXEC_PROPERTIES,
        shard_count = most_shards,
        tags = ["manual"] + ([
            "requires-kvm",
            "rbe-has-cgroup-v1-guest-variant",
        ] if platform == "kvm" else []),
    )
    for platform in _CONTAINER_PLATFORMS
]

# The cgroup-v1 fixture owns VM-wide controller bindings. Run it inside the
# disposable guest, never around QEMU on the Actions host. Six GiB includes
# the existing four-GB test budget plus guest kernel/initramfs overhead.
amd64_kvm_test(
    name = "container_test_kvm_cgroup_v1_guest",
    size = "large",
    args = ["--test_platforms=systrap,kvm"],
    guest_env = {"CGROUPV2": "false"},
    image = "//test/rbe/tcg:amd64_cgroup_v1_guest",
    memory_mib = 6144,
    payload = ":container_test_kvm",
    run_under = "//test/rbe:cgroup_v1",
    scratch_gib = 8,
    shard_count = 1,
    tags = [],
)

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
    embed = [":container"],
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
            "go-benchmark-contract",
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
