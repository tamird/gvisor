load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "fragmentation",
    srcs = [
        "fragmentation.go",
        "reassembler.go",
    ],
    visibility = [
        "//pkg/tcpip/network/ipv4:__pkg__",
        "//pkg/tcpip/network/ipv6:__pkg__",
    ],
    deps = [
        "//pkg/buffer",
        "//pkg/ilist",
        "//pkg/log",
        "//pkg/sync",
        "//pkg/tcpip",
        "//pkg/tcpip/header",
        "//pkg/tcpip/stack",
    ],
)

go_test(
    name = "fragmentation_test",
    size = "small",
    srcs = [
        "fragmentation_test.go",
        "main_test.go",
        "reassembler_test.go",
    ],
    library = ":fragmentation",
    deps = [
        "//pkg/buffer",
        "//pkg/refs",
        "//pkg/tcpip/faketime",
        "//pkg/tcpip/network/internal/testutil",
        "//pkg/tcpip/stack",
        "@com_github_google_go_cmp//cmp:go_default_library",
    ],
)
