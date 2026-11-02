load("@rules_python//python:defs.bzl", "py_test")
load("//tools:defs.bzl", "go_binary")

go_binary(
    name = "reporter",
    srcs = [
        "impair.go",
        "main.go",
    ],
    importpath = "gvisor.dev/gvisor/test/rbe/m003_pair/reporter",
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
    name = "screen",
    srcs = ["run.py"],
    args = [
        "screen",
        "$(rootpath :reporter)",
    ],
    data = [":reporter"],
    main = "run.py",
)

py_test(
    name = "comparison",
    srcs = ["run.py"],
    args = ["compare"] + ["$(rootpath :%s)" % f for f in [
        "stock",
        "deadline",
        "select",
        "both",
        "screen.json",
    ]],
    data = [
        "both",
        "deadline",
        "screen.json",
        "select",
        "stock",
    ],
    main = "run.py",
)
