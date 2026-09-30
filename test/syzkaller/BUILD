load("@rules_oci//oci:defs.bzl", "oci_load")
load("@rules_python//python:defs.bzl", "py_binary")
load("//test/docker:defs.bzl", "docker_command_test", "docker_daemon_config")
load("//tools/bazeldefs:platforms.bzl", "docker_test_exec_properties")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

oci_load(
    name = "syzbot",
    image = "@syzkaller_syzbot_amd64",
    repo_tags = ["gvisor.dev/images/syzkaller:contract"],
    tags = ["manual"],
)

filegroup(
    name = "syzbot_archive",
    srcs = [":syzbot"],
    output_group = "tarball",
)

docker_daemon_config(
    name = "docker_config",
    testonly = True,
    images = [":syzbot_archive"],
)

py_binary(
    name = "make_contract",
    testonly = True,
    srcs = ["make_contract.py"],
)

docker_command_test(
    name = "make_contract_test",
    size = "large",
    timeout = "eternal",
    command = ":make_contract",
    command_args = [
        "$(rootpath //:Makefile)",
        "$(rootpath :docker_config)",
    ],
    data = [
        ":docker_config",
        "//:Makefile",
        "//tools:bazel.mk",
        "//tools:images.mk",
    ],
    docker_config = ":docker_config",
    exec_properties = docker_test_exec_properties(
        free_disk = "40GB",
        memory = "8GB",
    ),
    tags = ["manual"],
    target_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
)
