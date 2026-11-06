load("//tools:defs.bzl", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

platform(
    name = "linux_amd64_worker",
    exec_properties = {
        "Arch": "amd64",
        "OSFamily": "linux",
        "container-image": "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02",
        "workload-isolation-type": "oci",
    },
    parents = ["@io_bazel_rules_go//go/toolchain:linux_amd64_cgo"],
    visibility = ["//visibility:public"],
)

[go_test(
    name = isolation + "_test",
    size = "small",
    srcs = ["mode_test.go"],
    env = {"TEST_ISOLATION": isolation},
    exec_properties = {
        "test.dockerUser": "root",
        "test.network-enable-ipv6": "true",
        "test.workload-isolation-type": "firecracker",
    } if isolation == "firecracker" else {},
    pure = True,
    deps = [
        "//runsc/cgroup",
        "@org_golang_x_sys//unix:go_default_library",
    ],
) for isolation in ["oci", "firecracker"]]
