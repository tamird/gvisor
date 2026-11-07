package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//:sandbox"],
    licenses = ["notice"],
)

config_setting(
    name = "source_presubmit",
    define_values = {"source_presubmit": "true"},
)

_UBUNTU_WORKER = {
    "OSFamily": "linux",
    "container-image": "docker://docker.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02",
    "workload-isolation-type": "oci",
}

[
    platform(
        name = "ubuntu_" + arch,
        exec_properties = dict(_UBUNTU_WORKER, Arch = arch),
        parents = ["//tools/bazeldefs:linux_" + arch],
    )
    for arch in ["amd64", "arm64"]
]
