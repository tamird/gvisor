load("//runsc:sidecar.bzl", "freestanding_sidecar")
load(":defs.bzl", "native_result", "observation")

freestanding_sidecar(
    name = "worker_identity",
    src = "worker_identity.c",
    out = "worker-identity",
)

[
    observation(
        name = "on_" + arch,
        exec_compatible_with = ["@platforms//os:linux", "@platforms//cpu:" + cpu],
        identity = ":worker_identity",
        sidecar = "//runsc/fdparking:fdparking_binary",
    )
    for arch, cpu in [("amd64", "x86_64"), ("arm64", "aarch64")]
]

[
    native_result(name = "fdparking_" + arch, arch = arch, observation = ":on_" + arch)
    for arch in ["amd64", "arm64"]
]
