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

# The zero-layer image has no /tmp. BuildBuddy mounts the action workspace
# here, including for tools that supply their own action environment.
# Keep the test group's Ubuntu image and its ordinary temporary directory.
# Use the target platform as parent: a parent with execution properties
# would remove the empty test overrides before Bazel selects the exec group.
[
    platform(
        name = "scratch_build_" + arch,
        constraint_values = ["@llvm//constraints/libc:musl"],
        exec_properties = dict(_UBUNTU_WORKER, Arch = arch) | {
            "container-image": "docker://docker.io/tianon/scratch@sha256:" + digest,
            "env-overrides": "TMPDIR=/buildbuddy-execroot",
            "execroot-path": "/buildbuddy-execroot",
            "test.container-image": _UBUNTU_WORKER["container-image"],
            "test.env-overrides": "",
            "test.execroot-path": "",
        },
        parents = ["//tools/bazeldefs:linux_" + arch],
    )
    for arch, digest in [
        ("amd64", "3d455195f1141b05cb8cc6deb07ea96b617e5436c7a259d9f3e7b1985f4d3f51"),
        ("arm64", "0fe9f0d1914ac271ceffe2ff90d691c5a4dfcc2859928cdcf3561381a21ac5be"),
    ]
]
