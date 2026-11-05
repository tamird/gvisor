load(":wrapper.bzl", "test_wrapper_files")

# Native tool-free workers. The product's GNU target platform is unchanged.
[
    platform(
        name = arch,
        constraint_values = ["@llvm//constraints/libc:musl"],
        parents = ["@io_bazel_rules_go//go/toolchain:linux_%s_cgo" % arch],
        exec_properties = {
            "Arch": arch,
            "OSFamily": "linux",
            "container-image": "docker://gcr.io/distroless/static-debian13@sha256:" + digest,
            "recycle-runner": "false",
            "workload-isolation-type": "oci",
        },
        visibility = ["//visibility:public"],
    )
    for arch, digest in [
        ("amd64", "d03c145fad326bff275ec4106475ed8310426caf709decd9f8cbc494af59bc18"),
        ("arm64", "b0aca6ddfad947e66782a29534647936d4d645a1b96d9c8a97e4f53b1607d428"),
    ]
]

test_wrapper_files(name = "stock_test_wrapper")
