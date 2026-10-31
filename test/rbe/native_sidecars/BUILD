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
