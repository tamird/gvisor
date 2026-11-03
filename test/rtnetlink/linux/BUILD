load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("@rules_shell//shell:sh_library.bzl", "sh_library")
load("//tools:defs.bzl", "go_binary")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = [
        "//:sandbox",
    ],
    licenses = ["notice"],
)

sh_library(
    name = "rtnetlink_test",
    srcs = ["rtnetlink_test.sh"],
)

sh_binary(
    name = "veth_test",
    srcs = ["veth_test.sh"],
    deps = [":rtnetlink_test"],
)

sh_binary(
    name = "setlink_test",
    srcs = ["setlink_test.sh"],
    deps = [":rtnetlink_test"],
)

sh_binary(
    name = "route_test",
    srcs = ["route_test.sh"],
    deps = [":rtnetlink_test"],
)

sh_binary(
    name = "bridge_test",
    srcs = ["bridge_test.sh"],
    data = [
        "short_connections.py",
        ":tcp_serv",
    ],
    deps = [
        ":rtnetlink_test",
    ],
)

go_binary(
    name = "tcp_serv",
    srcs = ["tcp_serv.go"],
    pure = True,
    deps = [
        "@org_golang_x_sys//unix:go_default_library",
    ],
)


# Validate error capture before spending time on either runsc binary.
genrule(
    name = "short_bridge_error_canary",
    outs = ["short_bridge_canary_output"],
    cmd = "echo SHORT_BRIDGE_EXPECTED_FAILURE >&2; exit 41",
    tags = ["local", "manual"],
)

# The native control does not need the ordinary syscall runner's //:release.
sh_test(
    name = "short_bridge_native_canary",
    srcs = ["bridge_test.sh"],
    data = ["short_connections.py"],
    deps = [":rtnetlink_test"],
    timeout = "long",
)

# These local-only tests consume digest-verified official release packages.
[
    sh_test(
        name = "short_bridge_" + version,
        srcs = ["packaged_bridge.py"],
        args = [version],
        data = [
            version + ".deb",
            "baseline-bridge.sh",
            "baseline-short.py",
            "bridge_test.sh",
            "rtnetlink_test.sh",
            "short_connections.py",
        ],
        timeout = "long",
        tags = ["local", "manual"],
    )
    for version in ["july", "current"]
]

# Local refers to the disposable Actions Linux host, not an RBE worker's Docker daemon.
sh_test(
    name = "nested_docker_native",
    srcs = ["nested_docker.py"],
    data = [
        "dind-rootfs.tar",
        "dind_guest.sh",
        "dind_socket.py",
        "packaged_bridge.py",
        "python-image.tar",
    ],
    tags = ["local", "manual"],
    timeout = "long",
)

sh_test(
    name = "veth_peer_mtu",
    srcs = ["veth_peer_mtu.py"],
    tags = ["local", "manual"],
)
