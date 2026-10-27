load("@python_distribution_deps//:requirements.bzl", "requirement")
load("@rules_python//python:defs.bzl", "py_binary")
load(":build.bzl", "qemu_system")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

exports_files(["source.BUILD"])

py_binary(
    name = "build",
    srcs = ["build.py"],
    main = "build.py",
    deps = [
        requirement("setuptools"),
        requirement("wheel"),
    ],
)

qemu_system(
    name = "system_aarch64",
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
    visibility = [
        "//:__pkg__",
        "//pkg/coretag:__pkg__",
        "//pkg/pinring:__pkg__",
        "//pkg/sentry/platform/kvm:__pkg__",
        "//runsc/cmd:__pkg__",
        "//runsc/container:__pkg__",
        "//runsc/procumounter:__pkg__",
        "//test:__subpackages__",
    ],
)
