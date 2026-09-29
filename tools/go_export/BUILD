load("@rules_python//python:defs.bzl", "py_binary")
load("//tools:defs.bzl", "build_test")
load(":defs.bzl", "module_build")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

py_binary(
    name = "compile",
    srcs = ["compile.py"],
)

# The public CI netstack portability selection. The Linux build also compiles
# all exported packages, including the command entrypoints.
_NETSTACK_PACKAGES = [
    "./pkg/tcpip",
    "./pkg/tcpip/adapters/gonet",
    "./pkg/tcpip/header",
    "./pkg/tcpip/link/channel",
    "./pkg/tcpip/network/ipv4",
    "./pkg/tcpip/network/ipv6",
    "./pkg/tcpip/stack",
    "./pkg/tcpip/transport/icmp",
    "./pkg/tcpip/transport/tcp",
    "./pkg/tcpip/transport/udp",
    "./pkg/buffer",
    "./pkg/waiter",
]

module_build(
    name = "linux_amd64",
    cgo = True,
    goarch = "amd64",
    goos = "linux",
    packages = ["./..."],
    tags = ["manual"],
    target_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
)

_NETSTACK_PLATFORMS = [
    ("darwin", "arm64"),
    ("windows", "amd64"),
    ("freebsd", "amd64"),
    ("openbsd", "amd64"),
    ("linux", "mips"),
]

[
    module_build(
        name = goos + "_" + goarch,
        goarch = goarch,
        goos = goos,
        packages = _NETSTACK_PACKAGES,
        tags = ["manual"],
    )
    for goos, goarch in _NETSTACK_PLATFORMS
]

_PLATFORMS = ["linux_amd64"] + [goos + "_" + goarch for goos, goarch in _NETSTACK_PLATFORMS]

[
    build_test(
        name = platform + "_test",
        tags = ["manual"],
        targets = [":" + platform],
        visibility = ["//visibility:public"],
    )
    for platform in _PLATFORMS
]

test_suite(
    name = "all_test",
    tags = ["manual"],
    tests = [":" + platform + "_test" for platform in _PLATFORMS],
    visibility = ["//visibility:public"],
)
