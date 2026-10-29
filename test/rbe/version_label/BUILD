load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("//runsc/version:defs.bzl", "VERSION_X_DEFS")
load("//tools:defs.bzl", "go_binary")

exports_files(["release.tar.bz2", "ordinary.tar.bz2", "ordinary.label"])

go_binary(
    name = "guest",
    srcs = ["guest.go"],
    pure = True,
)

sh_test(
    name = "restore",
    size = "large",
    srcs = ["restore.sh"],
    args = [
        "$(rootpath :guest)",
        "$(rootpath :release.tar.bz2)",
        "$(rootpath :ordinary.tar.bz2)",
        "$(rootpath :ordinary.label)",
    ],
    data = [":guest", ":release.tar.bz2", ":ordinary.tar.bz2", ":ordinary.label"],
    exec_properties = {
        "test.Arch": "amd64",
        "test.OSFamily": "linux",
        "test.EstimatedCPU": "4",
        "test.EstimatedMemory": "8GB",
        "test.EstimatedFreeDiskBytes": "30GB",
        "test.container-image": "docker://gcr.io/flame-public/rbe-ubuntu22-04@sha256:0d84a80bb0fc36ba5381942adcf6493249594dcc9044845c617b78c9b621cae3",
        "test.dockerUser": "root",
        "test.workload-isolation-type": "firecracker",
        "test.recycle-runner": "false",
    },
    tags = ["manual", "no-local"],
)

go_binary(
    name = "probe",
    srcs = ["probe.go"],
    pure = True,
    x_defs = VERSION_X_DEFS,
    deps = ["//runsc/version"],
)
