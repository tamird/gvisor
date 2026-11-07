load("@io_bazel_rules_go//go:def.bzl", "go_test")

[
    go_test(
        name = arch,
        srcs = ["identity_test.go"],
        data = ["//:release"],
        exec_compatible_with = ["@platforms//cpu:" + cpu],
        exec_properties = {
            "Arch": arch,
            "OSFamily": "linux",
            "container-image": "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02",
            "workload-isolation-type": "oci",
        },
        target_compatible_with = ["@platforms//cpu:" + cpu],
    )
    for arch, cpu in [("amd64", "x86_64"), ("arm64", "aarch64")]
]
