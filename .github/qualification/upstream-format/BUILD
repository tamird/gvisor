load("@buildifier_prebuilt//:rules.bzl", "buildifier_binary", "buildifier_test")

buildifier_binary(name = "cli")

[
    buildifier_test(
        name = stem,
        srcs = [stem + ".bzl"],
        mode = "check",
        size = "small",
    )
    for stem in ["clean", "dirty"]
]

[
    platform(
        name = name,
        constraint_values = ["@platforms//cpu:x86_64", "@platforms//os:linux"],
        exec_properties = {
            "Arch": "amd64",
            "OSFamily": "linux",
            "container-image": image,
            "workload-isolation-type": "oci",
        },
    )
    for name, image in [
        ("ubuntu", "docker://mirror.gcr.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02"),
        ("scratch", "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef"),
    ]
]
