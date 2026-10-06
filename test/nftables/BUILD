load("//test/docker:config.bzl", "NFTABLES_VARIANTS")
load("//test/docker:defs.bzl", "docker_command_test", "docker_daemon_config", "docker_test")
load("//tools:arch.bzl", "select_arch")
load("//tools:defs.bzl", "go_library")
load("//tools/bazeldefs:platforms.bzl", "docker_test_exec_properties")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "nftables",
    testonly = 1,
    srcs = [
        "filter_input.go",
        "filter_input_iptables.go",
        "iptables_util.go",
        "nftables.go",
        "nftables_util.go",
        "nftables_validation.go",
    ],
    visibility = ["//test/nftables:__subpackages__"],
    deps = [
        "//pkg/log",
        "//test/netutils",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

docker_test(
    name = "nftables_test",
    size = "large",
    srcs = ["nftables_test.go"],
    cohort = "nftables",
    data = ["//test/nftables/runner"],
    ipv6 = True,
    library = ":nftables",
    owned_args = ["--test-net-raw=true"],
    runtime_variants = NFTABLES_VARIANTS,
    tags = [
        "manual",
    ],
    deps = [
        "//pkg/log",
        "//pkg/test/dockerutil",
        "//test/netutils/container",
    ],
)

docker_daemon_config(
    name = "native_syscall_docker_config",
    testonly = True,
    images = select_arch(
        amd64 = ["//test/docker:images_nftables_amd64_tar"],
        arm64 = ["//test/docker:images_nftables_arm64_tar"],
    ),
    ipv6 = True,
    tags = ["manual"],
)

docker_command_test(
    name = "native_syscall_test",
    size = "large",
    command = "native_syscall_test.sh",
    command_args = ["$(rootpath //test/syscalls/linux:socket_netlink_netfilter_test)"],
    data = ["//test/syscalls/linux:socket_netlink_netfilter_test"],
    docker_config = ":native_syscall_docker_config",
    exec_properties = docker_test_exec_properties(free_disk = "20GB"),
    tags = ["manual"],
)

test_suite(
    name = "owned_tests",
    tags = ["manual"],
    tests = [
        ":native_syscall_test",
        ":nftables_test_owned",
    ],
)
