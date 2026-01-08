load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_test(
    name = "hostarch_test",
    size = "small",
    srcs = [
        "addr_range_seq_test.go",
        "addr_test.go",
    ],
    library = ":hostarch",
)

go_library(
    name = "hostarch",
    srcs = [
        "access_type.go",
        "addr.go",
        "addr_range_seq_unsafe.go",
        "addr_unsafe.go",
        "hostarch.go",
        "hostarch_arm64.go",
        "hostarch_x86.go",
        "memory_type.go",
        "sizes_util.go",
    ],
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/gohacks",
        "//pkg/segment",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)
