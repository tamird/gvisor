load("@rules_python//python:defs.bzl", "py_binary")
load(":build.bzl", "linux_kernel")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

py_binary(
    name = "build",
    srcs = ["build.py"],
    main = "build.py",
)

linux_kernel(
    name = "kcov",
    exec_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    exec_properties = {
        "EstimatedCPU": "16",
        "EstimatedMemory": "32GB",
        "EstimatedFreeDiskBytes": "40GB",
        "default-timeout": "45m",
    },
    release = "7.3.0-rc3-gvisor-kcov",
    visibility = ["//test/rbe/tcg:__pkg__"],
)
