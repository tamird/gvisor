load("//tools:defs.bzl", "go_test")

exports_files(["main-observations.patch", "wire-observations.patch"])

go_test(
    name = "heavy_loss",
    size = "large",
    srcs = [
        "observe.go",
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
