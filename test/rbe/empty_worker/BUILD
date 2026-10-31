load(":probe.bzl", "observed_root")

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
    parents = ["@io_bazel_rules_go//go/toolchain:linux_amd64_cgo"],
    exec_properties = {
        "Arch": "amd64",
        "OSFamily": "linux",
        "container-image": "docker://gcr.io/gvisor-presubmit/default_x86_64@sha256:cff9bcf01c7a23182b2bb44520cfeb3375b6b138ddca3ab2e121bc54015e754a",
        "workload-isolation-type": "firecracker",
    },
    visibility = ["//visibility:public"],
)
