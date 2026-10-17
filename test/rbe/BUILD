# Validation-only platforms; the source MODULE graph stays unchanged.
package(default_applicable_licenses = ["//:license"])

platform(
    name = "linux_amd64",
    exec_properties = {
        "Arch": "amd64",
        "OSFamily": "linux",
        "container-image": "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02",
        "workload-isolation-type": "oci",
    },
    parents = ["@io_bazel_rules_go//go/toolchain:linux_amd64_cgo"],
    visibility = ["//visibility:public"],
)

platform(
    name = "linux_arm64",
    exec_properties = {
        "Arch": "arm64",
        "OSFamily": "linux",
        "container-image": "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02",
        "workload-isolation-type": "oci",
    },
    parents = ["@io_bazel_rules_go//go/toolchain:linux_arm64_cgo"],
    visibility = ["//visibility:public"],
)
