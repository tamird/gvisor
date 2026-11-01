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
