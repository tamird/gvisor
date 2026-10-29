package(default_visibility = ["//visibility:public"])

[
    platform(
        name = arch,
        constraint_values = ["@platforms//os:linux", "@platforms//cpu:" + cpu],
        exec_properties = {
            "Arch": arch,
            "OSFamily": "linux",
            "container-image": "docker://mirror.gcr.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02",
            "lint.container-image": "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef",
            "workload-isolation-type": "oci",
        },
    )
    for arch, cpu in [("amd64", "x86_64"), ("arm64", "aarch64")]
]
