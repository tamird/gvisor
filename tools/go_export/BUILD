load("@rules_python//python:defs.bzl", "py_binary")
load("@with_cfg.bzl//:with_cfg.bzl", "original_settings")
load("//tools:defs.bzl", "build_test", "go_binary", "go_test")
load(":defs.bzl", "cgo_module_build", "module_build")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_binary(
    name = "assemble",
    srcs = ["main.go"],
    visibility = ["//:__pkg__"],
    deps = ["@org_golang_x_mod//modfile:go_default_library"],
)

go_test(
    name = "assemble_test",
    size = "small",
    srcs = [
        "main.go",
        "main_test.go",
    ],
    deps = ["@org_golang_x_mod//modfile:go_default_library"],
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

original_settings(name = "cgo_original_settings")

cgo_module_build(
    name = "linux_amd64",
    # with_cfg resets explicit attributes, not the underlying rule's defaults.
    archive = "//:go_export",
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
