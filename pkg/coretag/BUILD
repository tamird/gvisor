load("//test/rbe/tcg:defs.bzl", "amd64_kvm_test")
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
    ],
)

# Exercise PR_SCHED_CORE with two SMT siblings in the existing guest.
amd64_kvm_test(
    name = "coretag_smt_kvm_guest",
    size = "small",
    amd64_smt = True,
    image = "//test/rbe/tcg:amd64_rc_guest",
    payload = ":coretag_test_amd64",
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
