load(":native.bzl", "native_release")

# Native tool-free workers. The product's GNU target platform is unchanged.
[
    platform(
        name = arch,
        constraint_values = ["@llvm//constraints/libc:musl"],
        parents = ["@io_bazel_rules_go//go/toolchain:linux_%s_cgo" % arch],
        exec_properties = {
            "Arch": arch,
            "OSFamily": "linux",
            "container-image": "docker://cgr.dev/chainguard/static@sha256:fc42af33e643f20d08baf03cddcc1c02ba57052dfb02edb0d79f3fc47bcea8d9",
            "recycle-runner": "false",
            "workload-isolation-type": "oci",
        },
        visibility = ["//visibility:public"],
    )
    for arch in ["amd64", "arm64"]
]

[
    native_release(
        name = "release_" + arch,
        arch = arch,
        archive = "//debian:gvisor-release-tar-bz2",
        runsc = "//runsc",
        sentry = "//runsc/cmd/sentry:gvisor_sentry",
    )
    for arch in ["amd64", "arm64"]
]
