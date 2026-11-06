# The original musl platforms remain first for compiler/tool execution.
# Static GNU test platforms describe the self-contained target ABI on the same
# workers; the workers provide no GNU dynamic loader or runtime overlay.
[
    platform(
        name = arch + suffix,
        constraint_values = [] if suffix else ["@llvm//constraints/libc:musl"],
        parents = ["@llvm//platforms:linux_%s_glibc_static" % cpu] if suffix else ["@io_bazel_rules_go//go/toolchain:linux_%s_cgo" % arch],
        exec_properties = {
            "Arch": arch,
            "OSFamily": "linux",
            "container-image": "docker://gcr.io/distroless/static-debian13@sha256:" + digest,
            "recycle-runner": "false",
            "workload-isolation-type": "oci",
        },
        visibility = ["//visibility:public"],
    )
    for arch, cpu, digest in [
        ("amd64", "x86_64", "d03c145fad326bff275ec4106475ed8310426caf709decd9f8cbc494af59bc18"),
        ("arm64", "aarch64", "b0aca6ddfad947e66782a29534647936d4d645a1b96d9c8a97e4f53b1607d428"),
    ]
    for suffix in ["", "_static_tests"]
]

