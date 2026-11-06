load("@bazel_skylib//rules:native_binary.bzl", "native_test")

exports_files([
    "framework.patch",
    "vfio_api_probe.c",
    "vfio_probe.mk",
])

native_test(
    name = "external_traffic",
    size = "medium",
    src = "external_traffic.py",
    local = True,
)
