load("//test/docker:defs.bzl", "docker_daemon_config")
load("//test/packetdrill:defs.bzl", "packetdrill_test")
load("//tools:arch.bzl", "select_arch")
load("//tools:defs.bzl", "bzl_library")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

docker_daemon_config(
    name = "docker_config",
    testonly = True,
    images = select_arch(
        amd64 = ["//test/docker:images_packetdrill_amd64_tar"],
        arm64 = ["//test/docker:images_packetdrill_arm64_tar"],
    ),
    tags = ["manual"],
)

packetdrill_test(
    name = "packetdrill_sanity_test",
    scripts = ["sanity_test.pkt"],
)

packetdrill_test(
    name = "accept_ack_drop_test",
    scripts = ["accept_ack_drop.pkt"],
)

packetdrill_test(
    name = "fin_wait2_timeout_test",
    scripts = ["fin_wait2_timeout.pkt"],
)

packetdrill_test(
    name = "listen_close_before_handshake_complete_test",
    scripts = ["listen_close_before_handshake_complete.pkt"],
)

packetdrill_test(
    name = "no_rst_to_rst_test",
    scripts = ["no_rst_to_rst.pkt"],
)

packetdrill_test(
    name = "tcp_defer_accept_test",
    scripts = ["tcp_defer_accept.pkt"],
)

packetdrill_test(
    name = "tcp_defer_accept_timeout_test",
    scripts = ["tcp_defer_accept_timeout.pkt"],
)

packetdrill_test(
    name = "cubic_fast_recovery_test",
    scripts = ["cubic_fast_recovery.pkt"],
)

packetdrill_test(
    name = "cubic_rto_recovery_test",
    scripts = ["cubic_rto_recovery.pkt"],
)

test_suite(
    name = "all_tests",
    tags = [
        "local",
        "manual",
        "packetdrill",
    ],
    tests = [name for name in existing_rules() if name.endswith("_test")],
)

test_suite(
    name = "owned_tests",
    tags = [
        "manual",
        "packetdrill",
    ],
    tests = [name for name in existing_rules() if name.endswith("_owned")],
)

bzl_library(
    name = "defs_bzl",
    srcs = ["defs.bzl"],
    visibility = ["//visibility:private"],
    deps = [
        "//tools/bazeldefs:platforms_bzl",
        "@bazel_skylib//lib:shell",
    ],
)
