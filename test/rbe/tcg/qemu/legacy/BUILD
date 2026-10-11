load("@rules_python//python:defs.bzl", "py_binary")
load("//tools:defs.bzl", "bzl_library")
load(":build.bzl", "qemu_pair")

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
    visibility = ["//test/rbe/tcg/qemu:__pkg__"],
)
