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
    visibility = ["//test:__subpackages__"],
)

# Fork-only matched emulator cost control.
py_binary(
    name = "run_pair",
    srcs = ["run_pair.py"],
    main = "run_pair.py",
)
