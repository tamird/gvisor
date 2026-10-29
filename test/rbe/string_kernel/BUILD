load("@rules_shell//shell:sh_test.bzl", "sh_test")

sh_test(
    name = "nginx",
    size = "large",
    srcs = ["nginx.sh"],
    args = ["$(rootpath //test/benchmarks/checkpointfile:checkpointfile_test)", "$(rootpath :nginx.tar)"],
    data = [
        ":nginx.tar",
        "//:release",
        "//test/benchmarks/checkpointfile:checkpointfile_test",
    ],
    exec_properties = {
        "test.Arch": "amd64",
        "test.OSFamily": "linux",
        "test.EstimatedCPU": "4",
        "test.EstimatedMemory": "8GB",
        "test.EstimatedFreeDiskBytes": "40GB",
        "test.container-image": "docker://gcr.io/flame-public/rbe-ubuntu22-04@sha256:0d84a80bb0fc36ba5381942adcf6493249594dcc9044845c617b78c9b621cae3",
        "test.dockerUser": "root",
        "test.network": "external",
        "test.workload-isolation-type": "firecracker",
        "test.recycle-runner": "false",
    },
    tags = ["manual", "no-local"],
)
