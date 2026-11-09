load("//tools:defs.bzl", "namespace_test_exec_properties")
load("//tools/bazeldefs:presubmit_tests.bzl", "native_test")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//:sandbox"],
    licenses = ["notice"],
)

config_setting(
    name = "source_presubmit",
    define_values = {"source_presubmit": "true"},
)

config_setting(
    name = "mixed_native_tests",
    define_values = {"mixed_native_tests": "true"},
)

test_suite(
    name = "uname",
    tags = ["manual"],
    tests = [
        "//test/syscalls:uname_test_native",
        "//test/syscalls:uname_test_native_presubmit_arm64",
    ],
)

# This binary creates its own network namespace. Running it directly lets
# it retain the native worker's initial user namespace for privileged cases.
native_test(
    name = "netfilter_syscalls",
    src = "//test/syscalls/linux:socket_netlink_netfilter_test",
    exec_properties = namespace_test_exec_properties,
    tags = ["manual"],
)

test_suite(
    name = "native_syscalls",
    tags = ["manual"],
    tests = [
        ":netfilter_syscalls",
        ":netfilter_syscalls_presubmit_arm64",
        ":uname",
    ],
)

_UBUNTU_WORKER = {
    "OSFamily": "linux",
    "container-image": "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02",
    "workload-isolation-type": "oci",
}

[
    platform(
        name = "ubuntu_" + arch,
        exec_properties = dict(_UBUNTU_WORKER, Arch = arch),
        parents = ["//tools/bazeldefs:linux_" + arch],
    )
    for arch in ["amd64", "arm64"]
]

# Keep the namespace TestRunner on its original image. The same platform
# requests the shell- and loader-free image for ordinary producer actions.
[
    platform(
        name = "scratch_build_" + arch,
        constraint_values = ["@llvm//constraints/libc:musl"],
        exec_properties = {
            "container-image": "docker://gcr.io/distroless/static-debian13@sha256:" + digest,
            "test.container-image": _UBUNTU_WORKER["container-image"],
        },
        parents = [":ubuntu_" + arch],
    )
    for arch, digest in [
        ("amd64", "d03c145fad326bff275ec4106475ed8310426caf709decd9f8cbc494af59bc18"),
        ("arm64", "b0aca6ddfad947e66782a29534647936d4d645a1b96d9c8a97e4f53b1607d428"),
    ]
]
