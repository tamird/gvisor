load("//tools:defs.bzl", "go_binary", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_binary(
    name = "tun_tcp_connect",
    srcs = ["main.go"],
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/rawfile",
        "//pkg/tcpip",
        "//pkg/tcpip/header",
        "//pkg/tcpip/link/fdbased",
        "//pkg/tcpip/link/sniffer",
        "//pkg/tcpip/link/tun",
        "//pkg/tcpip/network/ipv4",
        "//pkg/tcpip/stack",
        "//pkg/tcpip/transport/tcp",
        "//pkg/waiter",
    ],
)

go_test(
    name = "tun_tcp_connect_test",
    srcs = ["main_test.go"],
    data = [":tun_tcp_connect"],
    deps = ["//pkg/test/testutil"],
)
