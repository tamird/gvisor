load("//test/rbe/tcg:defs.bzl", "arm64_tcg_test")
load("//tools:defs.bzl", "go_library", "go_test", "namespace_test_exec_properties", "native_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "pinring",
    srcs = [
        "pinring.go",
        "pinring_unsafe.go",
    ],
    importpath = "gvisor.dev/gvisor/pkg/pinring",
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/abi/linux",
        "//pkg/log",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

go_test(
    name = "pinring_test",
    size = "medium",
    srcs = ["pinring_test.go"],
    embed = [":pinring"],
    deps = [
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

# Exercise the raw-socket case with the capability its ordinary unit worker lacks.
native_test(
    name = "pinring_afpacket_privileged_test",
    size = "medium",
    src = ":pinring_test",
    args = ["-test.run=^TestAFPacketDeferredRelease$$"],
    env = {
        "GO_TEST_RUN_FROM_BAZEL": "1",
        "GO_TEST_WRAP_TESTV": "1",
    },
    exec_properties = namespace_test_exec_properties(),
    tags = ["manual"],
    visibility = ["//test/rbe:__pkg__"],
)

# KVM qualification selects this existing case on a worker with VM creation.
native_test(
    name = "pinring_kvm_test",
    size = "medium",
    src = ":pinring_test",
    args = ["-test.run=^TestKVMVMDeferredRelease$$"],
    env = {
        "GO_TEST_RUN_FROM_BAZEL": "1",
        "GO_TEST_WRAP_TESTV": "1",
    },
    exec_properties = namespace_test_exec_properties(),
    tags = [
        "manual",
        "requires-kvm",
    ],
    visibility = ["//test/rbe:__pkg__"],
)

arm64_tcg_test(
    name = "pinring_kvm_arm64_tcg_test",
    size = "large",
    args = ["-test.run=^TestKVMVMDeferredRelease$$"],
    arm64_el2 = True,
    guest_env = {
        "GO_TEST_RUN_FROM_BAZEL": "1",
        "GO_TEST_WRAP_TESTV": "1",
    },
    image = "//test/rbe/tcg:rc_guest",
    payload = ":pinring_test_arm64",
    tags = [],
    visibility = ["//test/rbe:__pkg__"],
)
