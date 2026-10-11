load("@rules_python//python:defs.bzl", "py_binary")
load("//tools:defs.bzl", "bzl_library")
load("//tools/bazeldefs:platforms.bzl", "RBE_DOCKER_TOOLS_IMAGE")
load(":build.bzl", "qemu_pair")
load(":pair.bzl", "qemu_pair_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

exports_files([
    "source.BUILD",
    "arm-rcsc.patch",
])

bzl_library(
    name = "repositories_bzl",
    srcs = ["repositories.bzl"],
)

py_binary(
    name = "build",
    srcs = ["build.py"],
    main = "build.py",
)

qemu_pair(
    name = "pair",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    exec_properties = {
        "EstimatedCPU": "8",
        "EstimatedMemory": "16GB",
        "default-timeout": "30m",
    },
    patch = "arm-rcsc.patch",
    tags = ["manual"],
)

py_binary(
    name = "run_pair",
    srcs = ["run_pair.py"],
    main = "run_pair.py",
)

# The diagnostic method selects 2100 seconds for these new pair owners only;
# run_pair.py preserves an independent 900-second deadline for each guest.
[
    qemu_pair_test(
        name = "epoll_" + order + "_test",
        size = "enormous",
        timeout = "eternal",
        archive = "//test/syscalls:epoll_test_runsc_systrap_directfs_64k_tcg_payload",
        exec_compatible_with = [
            "@platforms//os:linux",
            "@platforms//cpu:x86_64",
        ],
        exec_properties = {
            "test.EstimatedCPU": "2",
            "test.EstimatedMemory": "6GB",
            "test.EstimatedFreeDiskBytes": "8GB",
            "test.container-image": RBE_DOCKER_TOOLS_IMAGE,
            "test.dockerUser": "nobody",
            "test.nonroot-workspace": "true",
            "test.termination-grace-period": "30s",
            "test.workload-isolation-type": "oci",
        },
        order = [
            "before",
            "after",
        ] if order == "ab" else [
            "after",
            "before",
        ],
        payload = "//test/syscalls:epoll_test_runsc_systrap_directfs_64k_arm64",
        qemu = ":pair",
        tags = [
            "manual",
            "no-local",
        ],
    )
    for order in [
        "ab",
        "ba",
    ]
]
