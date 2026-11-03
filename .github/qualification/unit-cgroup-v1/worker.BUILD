load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
load("@rules_shell//shell:sh_test.bzl", "sh_test")

package(default_visibility = ["//visibility:public"])

# Compilation keeps the ordinary OCI image. Only tests mount v1 controllers.
platform(
    name = "worker",
    parents = ["//tools/bazeldefs:linux_amd64"],
    exec_properties = {
        "Arch": "amd64",
        "OSFamily": "linux",
        "container-image": "docker://gcr.io/flame-public/rbe-ubuntu22-04@sha256:0d84a80bb0fc36ba5381942adcf6493249594dcc9044845c617b78c9b621cae3",
        "workload-isolation-type": "oci",
        "test.EstimatedCPU": "4",
        "test.EstimatedMemory": "4GB",
        "test.EstimatedFreeDiskBytes": "20GB",
        "test.dockerUser": "root",
        "test.network": "external",
        "test.network-enable-ipv6": "true",
        "test.workload-isolation-type": "firecracker",
        "test.recycle-runner": "false",
    },
)

sh_binary(name = "wrapper", srcs = ["cgroup_v1.sh"])

sh_test(
    name = "preflight",
    srcs = ["cgroup_v1.sh"],
    args = ["$(rootpath :payload)"],
    data = [":payload"],
    size = "small",
)

sh_binary(name = "payload", srcs = ["payload.sh"], data = ["hook.bash"])
