# Use the common source-presubmit Ubuntu image for the action and test roles.
platform(
    name = "amd64",
    constraint_values = ["@llvm//constraints/libc:musl"],
    exec_properties = {
        "Arch": "amd64",
        "OSFamily": "linux",
        "container-image": "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02",
        "test.dockerUser": "root",
        "test.network-enable-ipv6": "true",
        "test.workload-isolation-type": "firecracker",
        "workload-isolation-type": "oci",
    },
    parents = ["//tools/bazeldefs:linux_amd64"],
    visibility = ["//visibility:public"],
)
