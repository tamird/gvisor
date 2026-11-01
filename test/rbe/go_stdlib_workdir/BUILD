load(":stdlib.bzl", "stdlib_metadata", "tmp_action")

stdlib_metadata(name = "metadata")

platform(
    name = "worker",
    constraint_values = ["@llvm//constraints/libc:musl"],
    parents = ["@io_bazel_rules_go//go/toolchain:linux_arm64_cgo"],
    exec_properties = {
        "Arch": "arm64",
        "OSFamily": "linux",
        "container-image": "docker://gcr.io/distroless/static-debian13@sha256:b0aca6ddfad947e66782a29534647936d4d645a1b96d9c8a97e4f53b1607d428",
        "recycle-runner": "false",
        "workload-isolation-type": "oci",
    },
    visibility = ["//visibility:public"],
)

tmp_action(name = "leave")

tmp_action(
    name = "inspect",
    previous = ":leave",
)
