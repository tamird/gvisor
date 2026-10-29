load("@io_bazel_rules_go//go:def.bzl", "go_test")

go_test(
    name = "study_test",
    size = "small",
    srcs = [
        "study_test.go",
        "@rack_reporter//:strip-timestamps.go",
    ],
    env = {
        "GOMAXPROCS": "1",
        "RACK_STUDY_POLICY": "data",
    },
    deps = [
        "//pkg/buffer",
        "//pkg/tcpip",
        "//pkg/tcpip/checksum",
        "//pkg/tcpip/header",
        "//pkg/tcpip/link/channel",
        "//pkg/tcpip/network/ipv4",
        "//pkg/tcpip/stack",
        "//pkg/tcpip/transport/tcp",
        "//pkg/waiter",
    ],
)
