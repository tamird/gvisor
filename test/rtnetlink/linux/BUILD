load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
load("@rules_shell//shell:sh_library.bzl", "sh_library")
load("@rules_shell//shell:sh_test.bzl", "sh_test")
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

# Fork qualification: exact source-built release and syscall binary.
sh_test(
    name = "source_bridge_mtu",
    srcs = ["packaged_bridge.py"],
    data = [
        "bridge-fixture",
        "bridge-release/containerd-shim-runsc-v1",
        "bridge-release/gvisor-bin/checkpointgofer",
        "bridge-release/gvisor-bin/gvisor-proc-umounter",
        "bridge-release/gvisor-bin/gvisor-sentry-prewarmer",
        "bridge-release/gvisor-bin/gvisor_sentry",
        "bridge-release/gvisor-bin/runsc-fd-parking",
        "bridge-release/gvisor-bin/runsc-metric-server",
        "bridge-release/runsc",
    ],
    tags = ["local", "manual"],
    timeout = "long",
)
