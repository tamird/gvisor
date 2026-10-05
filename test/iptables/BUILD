load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//test/docker:config.bzl", "NETFILTER_VARIANTS")
load("//test/docker:defs.bzl", "docker_command_test", "docker_daemon_config", "docker_test")
load("//tools:arch.bzl", "select_arch")
load("//tools:defs.bzl", "go_library")
load("//tools/bazeldefs:platforms.bzl", "docker_test_exec_properties")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "iptables",
    testonly = 1,
    srcs = [
        "filter_input.go",
        "filter_output.go",
        "iptables.go",
        "iptables_unsafe.go",
        "iptables_util.go",
        "nat.go",
        "reject.go",
    ],
    visibility = ["//test/iptables:__subpackages__"],
    deps = [
        "//pkg/binary",
        "//pkg/hostarch",
        "//pkg/test/testutil",
        "//test/netutils",
        "@com_github_vishvananda_netlink//:go_default_library",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

docker_test(
    name = "iptables_test",
    size = "large",
    srcs = [
        "iptables_test.go",
    ],
    cohort = "iptables",
    data = ["//test/iptables/runner"],
    ipv6 = True,
    library = ":iptables",
    owned_args = ["--test-net-raw=true"],
    runtime_variants = [NETFILTER_VARIANTS["iptables"]],
    tags = [
        "manual",
    ],
    deps = [
        "//pkg/log",
        "//pkg/test/dockerutil",
        "//pkg/test/testutil",
        "//test/netutils",
        "@com_github_vishvananda_netlink//:go_default_library",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

docker_test(
    name = "iptables_nft_test",
    size = "large",
    srcs = [
        "iptables_test.go",
    ],
    args = ["-iptables-nft=true"],
    cohort = "iptables",
    data = ["//test/iptables/runner"],
    ipv6 = True,
    library = ":iptables",
    # The legacy target owns analysis of these same test sources.
    nogo = False,
    owned_args = ["--test-net-raw=true"],
    runtime_variants = [NETFILTER_VARIANTS["nftables"]],
    tags = [
        "manual",
    ],
    deps = [
        "//pkg/log",
        "//pkg/test/dockerutil",
        "//pkg/test/testutil",
        "//test/netutils",
        "@com_github_vishvananda_netlink//:go_default_library",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

sh_test(
    name = "nftables_test",
    srcs = ["nftables_test.sh"],
    tags = [
        "local",
        "manual",
    ],
    visibility = ["//:sandbox"],
)

docker_daemon_config(
    name = "nftables_docker_config",
    testonly = True,
    images = select_arch(
        amd64 = ["//test/docker:images_iptables_amd64_tar"],
        arm64 = ["//test/docker:images_iptables_arm64_tar"],
    ),
    ipv6 = True,
    runtime_args = NETFILTER_VARIANTS["reproduce"].args,
    tags = ["manual"],
)

docker_command_test(
    name = "nftables_test_owned",
    command = "nftables_test.sh",
    docker_config = ":nftables_docker_config",
    exec_properties = docker_test_exec_properties(free_disk = "20GB"),
    tags = ["manual"],
)

test_suite(
    name = "owned_tests",
    tags = ["manual"],
    tests = [
        ":iptables_nft_test_owned",
        ":iptables_test_owned",
        ":nftables_test_owned",
    ],
)
