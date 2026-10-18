load("@rules_distroless//distroless:defs.bzl", "flatten")
load("@rules_python//python:defs.bzl", "py_binary")
load("@rules_shell//shell:sh_test.bzl", "sh_test")
load(":build.bzl", "linux_kernel", "linux_selftest")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

py_binary(
    name = "build",
    srcs = ["build.py"],
    main = "build.py",
)

_EXEC_COMPATIBLE_WITH = [
    "@platforms//os:linux",
    "@platforms//cpu:x86_64",
]

_EXEC_PROPERTIES = {
    # Explicit compute units keep measured or predicted task sizes from
    # reducing the resources requested for this parallel kernel build.
    "EstimatedComputeUnits": "16",
    "EstimatedMemory": "32GB",
    "EstimatedFreeDiskBytes": "40GB",
    "default-timeout": "45m",
}

linux_kernel(
    name = "kcov",
    exec_compatible_with = _EXEC_COMPATIBLE_WITH,
    exec_properties = _EXEC_PROPERTIES,
    release = "7.3.0-rc3-gvisor-kcov",
    visibility = ["//test/rbe/tcg:__pkg__"],
)

linux_kernel(
    name = "kcov_arm64",
    architecture = "arm64",
    exec_compatible_with = _EXEC_COMPATIBLE_WITH,
    exec_properties = _EXEC_PROPERTIES,
    release = "7.3.0-rc3-gvisor-kcov",
    visibility = ["//test/rbe/tcg:__pkg__"],
)

# The upstream x86 selftest library also builds VFIO helpers. Reuse the
# declared UUID headers and static library without changing its object list.
flatten(
    name = "selftest_sysroot",
    testonly = True,
    tars = [
        "@kernel_build_deps//:flat",
        "@qemu_build_deps//uuid-dev/amd64:data",
    ],
)

linux_selftest(
    name = "save_restore_pf_stress",
    testonly = True,
    exec_compatible_with = _EXEC_COMPATIBLE_WITH,
    exec_properties = {
        "EstimatedComputeUnits": "4",
        "EstimatedMemory": "8GB",
        "EstimatedFreeDiskBytes": "10GB",
        "default-timeout": "15m",
    },
    selftest = "x86/save_restore_pf_stress_test",
    tags = ["manual"],
    target_compatible_with = _EXEC_COMPATIBLE_WITH,
)

# Run on the Actions coordinator: the outer kernel implements the nested SVM
# path under investigation, regardless of the kernel running in the test VM.
sh_test(
    name = "nested_cr2_test",
    size = "small",
    timeout = "moderate",
    srcs = ["nested_cr2_test.sh"],
    args = ["$(rootpath :save_restore_pf_stress)"],
    data = [":save_restore_pf_stress"],
    local = True,
    tags = [
        "manual",
        "requires-kvm",
    ],
    target_compatible_with = _EXEC_COMPATIBLE_WITH,
)
