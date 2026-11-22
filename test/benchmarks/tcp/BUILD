load("//tools:defs.bzl", "cc_binary", "go_binary", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_binary(
    name = "tcp_proxy",
    srcs = [
        "tcp_proxy.go",
        "xdp.go",
    ],
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/rand",
        "//pkg/tcpip",
        "//pkg/tcpip/adapters/gonet",
        "//pkg/tcpip/link/fdbased",
        "//pkg/tcpip/link/qdisc/fifo",
        "//pkg/tcpip/link/sniffer",
        "//pkg/tcpip/link/xdp",
        "//pkg/tcpip/network/arp",
        "//pkg/tcpip/network/ipv4",
        "//pkg/tcpip/network/ipv6",
        "//pkg/tcpip/stack",
        "//pkg/tcpip/transport/icmp",
        "//pkg/tcpip/transport/tcp",
        "//pkg/tcpip/transport/udp",
        "//runsc/sandbox/bpf",
        "@com_github_cilium_ebpf//:go_default_library",
        "@com_github_cilium_ebpf//link:go_default_library",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

# nsjoin is a trivial replacement for nsenter. This is used because nsenter is
# not available on all systems where this benchmark is run (and we aim to
# minimize external dependencies.)

cc_binary(
    name = "nsjoin",
    srcs = ["nsjoin.c"],
    visibility = ["//:sandbox"],
)

go_binary(
    name = "tcp_benchmark",
    srcs = [
        "benchmark/benchmark.go",
        "benchmark/main.go",
    ],
    args = [
        "--nsjoin=$(rlocationpath :nsjoin)",
        "--tcp-proxy=$(rlocationpath :tcp_proxy)",
        "--iperf-binary=$(rlocationpath @iperf//:iperf3)",
    ],
    data = [
        ":nsjoin",
        ":tcp_proxy",
        "@iperf//:iperf3",
    ],
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/abi/linux",
        "@io_bazel_rules_go//go/runfiles",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

go_test(
    name = "cleanup_test",
    size = "medium",
    srcs = ["cleanup_test.go"],
    args = ["--iperf-binary=$(rootpath @iperf//:iperf3)"],
    data = [
        ":nsjoin",
        ":tcp_benchmark",
        ":tcp_proxy",
        "@iperf//:iperf3",
    ],
    local = True,
    tags = ["manual"],
    target_compatible_with = ["@platforms//os:linux"],
    deps = ["//pkg/test/testutil"],
)
