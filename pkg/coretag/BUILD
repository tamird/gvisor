load("//test/rbe/tcg:defs.bzl", "amd64_kvm_test", "arm64_tcg_test")
load("//tools:defs.bzl", "go_library", "go_test", "native_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "coretag",
    srcs = [
        "coretag.go",
        "coretag_unsafe.go",
    ],
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/abi/linux",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

go_test(
    name = "coretag_test",
    size = "small",
    srcs = [
        "coretag_test.go",
    ],
    embed = [":coretag"],
    pure = True,
    tags = ["nogotsan"],
    deps = [
        "//pkg/hostos",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

# Exercise PR_SCHED_CORE with two SMT siblings in the existing guest.
amd64_kvm_test(
    name = "coretag_smt_kvm_guest",
    size = "small",
    image = "//test/rbe/tcg:amd64_rc_guest",
    payload = ":coretag_test_amd64",
    smt = True,
    tags = ["nogotsan"],
)

arm64_tcg_test(
    name = "coretag_smt_arm64_tcg_test",
    size = "small",
    image = "//test/rbe/tcg:rc_guest",
    payload = ":coretag_test_arm64",
    smt = True,
    tags = ["nogotsan"],
)

# Reuse the local root fixture for the outer VM's /dev/kvm access.
native_test(
    name = "coretag_smt_kvm_test",
    size = "small",
    src = ":coretag_smt_kvm_guest",
    architectures = ["amd64"],
    tags = [
        "manual",
        "no-sandbox",
        "nogotsan",
        "requires-kvm",
    ],
)
