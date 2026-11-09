load("@rules_shell//shell:sh_test.bzl", "sh_test")

[
    platform(
        name = name,
        constraint_values = ["@platforms//os:linux", "@platforms//cpu:" + cpu],
        exec_properties = {
            "Arch": name,
            "OSFamily": "linux",
            "container-image": "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02",
            "workload-isolation-type": "oci",
        },
    )
    for name, cpu in [("amd64", "x86_64"), ("arm64", "aarch64")]
]

sh_test(
    name = "native",
    size = "small",
    srcs = ["native.sh"],
    args = ["$(rlocationpath //test/util:posix_error_test)"],
    data = ["//test/util:posix_error_test"],
    exec_compatible_with = ["@platforms//cpu:aarch64", "@platforms//os:linux"],
    exec_group_compatible_with = {"test": ["@platforms//cpu:aarch64"]},
)
