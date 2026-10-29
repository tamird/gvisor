package(default_visibility = ["//visibility:public"])

platform(
    name = "scratch",
    constraint_values = ["@platforms//cpu:aarch64", "@platforms//os:linux"],
    exec_properties = {
        "Arch": "arm64",
        "OSFamily": "linux",
        "container-image": "docker://mirror.gcr.io/library/hello-world@sha256:c567b231f65614688711a75f31518a064258a51d442117a3e771a3a8fdba78ef",
        "workload-isolation-type": "oci",
    },
)
