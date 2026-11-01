load(":probe.bzl", "observed_root")

platform(
    name = "arm64",
    parents = ["@io_bazel_rules_go//go/toolchain:linux_arm64_cgo"],
    exec_properties = {
        "Arch": "arm64",
        "OSFamily": "linux",
        "container-image": "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef",
        "workload-isolation-type": "oci",
    },
    visibility = ["//visibility:public"],
)


platform(
    name = "amd64",
    parents = ["@io_bazel_rules_go//go/toolchain:linux_amd64_cgo"],
    exec_properties = {
        "Arch": "amd64",
        "OSFamily": "linux",
        "container-image": "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef",
        "workload-isolation-type": "oci",
    },
    visibility = ["//visibility:public"],
)

observed_root(name = "root")

platform(
    name = "gnu",
    parents = ["@io_bazel_rules_go//go/toolchain:linux_arm64_cgo"],
    exec_properties = {
        "Arch": "arm64",
        "OSFamily": "linux",
        "container-image": "docker://mirror.gcr.io/library/gcc@sha256:66035d353338cb93b64f621393dc6fecde85258651ca454f0cf36ff2639b1352",
        "workload-isolation-type": "oci",
    },
    visibility = ["//visibility:public"],
)

exports_files([
    "cares-allow-symlinks.patch",
    "grpc-native.patch",
    "protobuf-stage1-direct.patch",
])

platform(
    name = "amd64_musl",
    constraint_values = ["@llvm//constraints/libc:musl"],
    parents = [":amd64"],
    visibility = ["//visibility:public"],
)

platform(
    name = "arm64_musl",
    constraint_values = ["@llvm//constraints/libc:musl"],
    parents = [":arm64"],
    visibility = ["//visibility:public"],
)

# Static glibc tests need the same Linux CPU ABI, but not the worker's libc.
# The execution platform can therefore keep using musl for its build tools.
[
    toolchain(
        name = "static_glibc_%s_test" % arch,
        exec_compatible_with = [
            "@platforms//cpu:" + cpu,
            "@platforms//os:linux",
        ],
        target_compatible_with = [
            "@llvm//constraints/glibc_linkage:static",
            "@llvm//constraints/libc:gnu.2.44",
            "@platforms//cpu:" + cpu,
            "@platforms//os:linux",
        ],
        toolchain = "@bazel_tools//tools/test:empty_toolchain",
        toolchain_type = "@bazel_tools//tools/test:default_test_toolchain_type",
        visibility = ["//visibility:public"],
    )
    for arch, cpu in [
        ("amd64", "x86_64"),
        ("arm64", "aarch64"),
    ]
]
