"""Keep the existing compiler owners in one multi-configuration build."""

load("@with_cfg.bzl//:with_cfg.bzl", "with_cfg")

_base = with_cfg(native.filegroup)

_amd64 = _base.clone().set("cpu", "k8").set(
    "platforms",
    [Label("//tools/bazeldefs:linux_amd64")],
)
_arm64 = _base.clone().set("cpu", "aarch64").set(
    "platforms",
    [Label("//tools/bazeldefs:linux_arm64")],
)

_amd64_normal, _amd64_normal_transition = _amd64.clone().set(
    Label("@io_bazel_rules_go//go/config:race"),
    False,
).set(Label("@io_bazel_rules_go//go/config:pure"), True).build()

_amd64_race, _amd64_race_transition = _amd64.clone().set(
    Label("@io_bazel_rules_go//go/config:race"),
    True,
).set(Label("@io_bazel_rules_go//go/config:pure"), False).build()

_arm64_normal, _arm64_normal_transition = _arm64.clone().set(
    Label("@io_bazel_rules_go//go/config:race"),
    False,
).set(Label("@io_bazel_rules_go//go/config:pure"), True).build()

_arm64_race, _arm64_race_transition = _arm64.clone().set(
    Label("@io_bazel_rules_go//go/config:race"),
    True,
).set(Label("@io_bazel_rules_go//go/config:pure"), False).build()

_RACE_TARGETS = [
    "//shim:containerd-shim-runsc-v1",
    "//test/benchmarks/base:startup_test",
    "//test/cmd/test_app:test_app",
    "//tools/gvisor2pcap:gvisor2pcap",
    "//tools/ioctl_sniffer:ioctl_hook_gnu",
    "//tools/ioctl_sniffer:run_sniffer",
    "//webhook:webhook",
]

_NORMAL_TARGETS = _RACE_TARGETS + [
    "//pkg/sentry/platform/systrap/sysmsg:sighandler_object",
    "//runsc/fdparking:fdparking_binary",
    "//runsc/prewarmer:prewarmer_binary",
    "//test/syscalls/linux:concurrency_test",
    "//test/syscalls/linux/rseq:rseq_binary",
    "//vdso:vdso",
]

def compiler_matrix(name):
    """Declare one default-output owner per existing target and configuration.

    Args:
      name: Prefix for the configured filegroups.
    """
    for suffix, configured, targets in [
        ("x86_64-normal", _amd64_normal, _NORMAL_TARGETS),
        ("x86_64-race", _amd64_race, _RACE_TARGETS),
        ("aarch64-normal", _arm64_normal, _NORMAL_TARGETS),
        ("aarch64-race", _arm64_race, _RACE_TARGETS),
    ]:
        for target in targets:
            configured(
                name = name + "_" + suffix + "_" + target.removeprefix("//").replace("/", "_").replace(":", "_"),
                testonly = True,
                srcs = [target],
                tags = ["manual"],
            )
