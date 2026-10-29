load("//tools:defs.bzl", "go_test")

go_test(
    name = "long_lived",
    size = "large",
    srcs = [
        "replay_test.go",
        "@tcp_replay_fixture//:sources",
    ],
    deps = [
        "//pkg/buffer",
        "//pkg/tcpip",
        "//pkg/tcpip/adapters/gonet",
        "//pkg/tcpip/checksum",
        "//pkg/tcpip/header",
        "//pkg/tcpip/link/channel",
        "//pkg/tcpip/network/ipv4",
        "//pkg/tcpip/network/ipv6",
        "//pkg/tcpip/stack",
        "//pkg/tcpip/transport/tcp",
        "//pkg/tcpip/transport/udp",
        "@org_golang_x_sys//unix",
    ],
)
