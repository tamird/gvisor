load("//tools:defs.bzl", "bzl_library", "go_binary", "go_test", "requires_cap_sys_admin")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//test/packetimpact:__subpackages__"],
    licenses = ["notice"],
)

bzl_library(
    name = "defs_bzl",
    srcs = ["defs.bzl"],
    visibility = ["//test/packetimpact:__subpackages__"],
)

go_test(
    name = "worker_test",
    size = "small",
    srcs = [
        "main.go",
        "worker_test.go",
    ],
    exec_properties = requires_cap_sys_admin(ipv6 = True),
    pure = True,
    tags = ["manual"],
    target_compatible_with = ["@platforms//os:linux"],
    deps = [
        "//test/packetimpact/dut",
        "//test/packetimpact/internal/testing",
        "//test/packetimpact/netdevs/netlink",
        "//test/packetimpact/testbench",
        "@com_github_google_gopacket//:go_default_library",
        "@com_github_google_gopacket//layers:go_default_library",
        "@com_github_google_gopacket//pcapgo:go_default_library",
        "@com_github_vishvananda_netlink//:go_default_library",
        "@org_golang_x_sync//errgroup:go_default_library",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

go_binary(
    name = "main",
    testonly = True,
    srcs = ["main.go"],
    visibility = [
        "//test/packetimpact:__subpackages__",
        "//turquoise/connectivity/netstack/gvisor_tests/packetimpact:__subpackages__",
    ],
    deps = [
        "//test/packetimpact/dut",
        "//test/packetimpact/internal/testing",
        "//test/packetimpact/netdevs/netlink",
        "//test/packetimpact/testbench",
        "@com_github_google_gopacket//:go_default_library",
        "@com_github_google_gopacket//layers:go_default_library",
        "@com_github_google_gopacket//pcapgo:go_default_library",
        "@com_github_vishvananda_netlink//:go_default_library",
        "@org_golang_x_sync//errgroup:go_default_library",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)
