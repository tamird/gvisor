load("//tools:defs.bzl", "go_library", "go_test")
load("//tools/go_generics:defs.bzl", "go_template_instance")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_template_instance(
    name = "udp_packet_list",
    out = "udp_packet_list.go",
    package = "udp",
    prefix = "udpPacket",
    template = "//pkg/ilist:generic_list",
    types = {
        "Element": "*udpPacket",
        "Linker": "*udpPacket",
    },
)

go_library(
    name = "udp",
    srcs = [
        "endpoint.go",
        "endpoint_state.go",
        "forwarder.go",
        "protocol.go",
        "udp_packet_list.go",
    ],
    visibility = ["//visibility:public"],
    deps = [
        "//pkg/buffer",
        "//pkg/log",
        "//pkg/sleep",
        "//pkg/sync",
        "//pkg/tcpip",
        "//pkg/tcpip/checksum",
        "//pkg/tcpip/header",
        "//pkg/tcpip/header/parse",
        "//pkg/tcpip/ports",
        "//pkg/tcpip/stack",
        "//pkg/tcpip/transport",
        "//pkg/tcpip/transport/internal/network",
        "//pkg/tcpip/transport/raw",
        "//pkg/waiter",
    ],
)

go_test(
    name = "udp_x_test",
    size = "small",
    srcs = ["udp_test.go"],
    deps = [
        ":udp",
        "//pkg/buffer",
        "//pkg/refs",
        "//pkg/tcpip",
        "//pkg/tcpip/checker",
        "//pkg/tcpip/checksum",
        "//pkg/tcpip/faketime",
        "//pkg/tcpip/header",
        "//pkg/tcpip/link/channel",
        "//pkg/tcpip/link/loopback",
        "//pkg/tcpip/network/ipv4",
        "//pkg/tcpip/network/ipv6",
        "//pkg/tcpip/stack",
        "//pkg/tcpip/testutil",
        "//pkg/tcpip/transport",
        "//pkg/tcpip/transport/icmp",
        "//pkg/tcpip/transport/testing/context",
        "//pkg/waiter",
    ],
)

# Common measurement-only fixture for the receive-list subtree.
go_test(
    name = "udp_test",
    size = "small",
    srcs = ["codec_queue_benchmark_test.go"],
    embed = [":udp"],
    deps = [
        "//pkg/buffer",
        "//pkg/state",
        "//pkg/tcpip",
        "//pkg/tcpip/header",
        "//pkg/tcpip/stack",
    ],
)

# Composition-only fresh 1x observation; the original benchmark body is unchanged.
go_test(
    name = "slice_diagnostic_udp_save",
    size = "small",
    srcs = ["codec_queue_benchmark_test.go"],
    architectures = [],
    args = [
        "-test.run=^$$",
        "-test.bench=^BenchmarkUDPReceiveQueueSave$$/^32$$",
        "-test.benchtime=1x",
        "-test.benchmem",
        "-test.count=1",
        "-test.cpu=1",
    ],
    embed = [":udp"],
    env = {
        "GODEBUG": "inittrace=1",
        "GOMAXPROCS": "1",
    },
    nogo = False,
    tags = ["manual"],
    deps = [
        "//pkg/buffer",
        "//pkg/state",
        "//pkg/tcpip",
        "//pkg/tcpip/header",
        "//pkg/tcpip/stack",
    ],
)
