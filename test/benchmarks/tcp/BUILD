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
        "benchmark/network.go",
    ],
    args = [
        "--nsjoin=$(rlocationpath :nsjoin)",
        "--tcp-proxy=$(rlocationpath :tcp_proxy)",
    ],
    data = [
        ":nsjoin",
        ":tcp_proxy",
    ],
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/abi/linux",
        "@io_bazel_rules_go//go/runfiles",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

go_test(
    name = "benchmark_test",
    srcs = [
        "benchmark/benchmark.go",
        "benchmark/benchmark_test.go",
        "benchmark/main.go",
        "benchmark/network.go",
    ],
    deps = [
        "//pkg/abi/linux",
        "@io_bazel_rules_go//go/runfiles",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

go_test(
    name = "benchmark_integration_test",
    size = "medium",
    srcs = [
        "benchmark/integration_test.go",
    ],
    args = [
        "--test-benchmark=$(rlocationpath :tcp_benchmark)",
        "--test-nsjoin=$(rlocationpath :nsjoin)",
        "--test-proxy=$(rlocationpath :tcp_proxy)",
    ],
    data = [
        ":nsjoin",
        ":tcp_benchmark",
        ":tcp_proxy",
    ],
    tags = ["requires-root"],
    deps = [
        "@io_bazel_rules_go//go/runfiles",
    ],
)
