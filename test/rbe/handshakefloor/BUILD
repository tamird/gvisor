load("@io_bazel_rules_go//go:def.bzl", "go_binary", "go_test")

go_binary(
    name = "reporter",
    srcs = [
        "@rack_reporter//:gso-split.go",
        "@rack_reporter//:gvisor-stacks.go",
        "@rack_reporter//:link-emulator.go",
        "@rack_reporter//:linux-sender.go",
        "@rack_reporter//:linux-sender-other.go",
        "@rack_reporter//:main.go",
        "@rack_reporter//:sender-state.go",
        "@rack_reporter//:strip-timestamps.go",
        "@rack_reporter//:wire-trace.go",
    ],
    pure = "on",
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
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

go_test(
    name = "study_test",
    size = "large",
    timeout = "eternal",
    srcs = ["study_test.go"],
    args = ["-reporter=$(rootpath :reporter)"],
    data = [":reporter"],
    deps = ["@org_golang_x_sys//unix:go_default_library"],
)
