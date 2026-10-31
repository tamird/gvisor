load("@rules_python//python:defs.bzl", "py_test")
load("//tools:defs.bzl", "go_binary")

go_binary(
    name = "reporter",
    srcs = ["@m003_source//:main.go"],
    importpath = "gvisor.dev/gvisor/test/rbe/m003/reporter",
    deps = [
        "//pkg/buffer",
        "//pkg/tcpip",
        "//pkg/tcpip/adapters/gonet",
        "//pkg/tcpip/header",
        "//pkg/tcpip/link/channel",
        "//pkg/tcpip/network/ipv4",
        "//pkg/tcpip/stack",
        "//pkg/tcpip/transport/tcp",
    ],
)

py_test(
    name = "m003_test",
    size = "large",
    srcs = ["m003_test.py"],
    args = ["$(rootpath :reporter)"],
    data = [":reporter"],
)
