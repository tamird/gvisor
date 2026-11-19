load("//tools:arch.bzl", "arch_genrule", "select_arch")
load("//tools:defs.bzl", "build_test", "go_library")
load("build.bzl", "cc_pie_obj", "sighandler_binary", "sighandler_go", "sighandler_object")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

cc_pie_obj(
    name = "sighandler",
    srcs = select_arch(
        amd64 = [
            "sighandler_amd64.c",
            "sysmsg_offsets_amd64.h",
        ],
        arm64 = ["sighandler_arm64.c"],
    ) + [
        "atomic.h",
        "sysmsg.h",
        "sysmsg_offsets.h",
    ],
    outs = ["sighandler.o"],
)

cc_pie_obj(
    name = "sysmsg_lib",
    srcs = [
        "atomic.h",
        "sysmsg.h",
        "sysmsg_lib.c",
        "sysmsg_offsets.h",
    ],
    outs = ["sysmsg_lib.o"],
)

cc_pie_obj(
    name = "sysrestorer",
    srcs = select_arch(
        amd64 = ["sigrestorer_amd64.S"],
        arm64 = ["sigrestorer_arm64.S"],
    ),
    outs = ["sysrestorer.o"],
)

cc_pie_obj(
    name = "syshandler",
    srcs = select_arch(
        amd64 = [
            "syshandler_amd64.S",
            "sysmsg_offsets_amd64.h",
        ],
        arm64 = ["syshandler_arm64.S"],
    ) + [
        "sysmsg_offsets.h",
    ],
    outs = ["syshandler.o"],
)

sighandler_object(
    name = "sighandler_object",
    out = "sighandler.built-in.bin.o",
    linker_script = "pie.lds.S",
    ordered_objects = [
        ":sysrestorer",
        ":sighandler",
        ":syshandler",
        ":sysmsg_lib",
    ],
)

sighandler_binary(
    name = "sighandler_binary",
    src = ":sighandler_object",
    out = "sighandler.built-in.bin",
)

arch_genrule(
    name = "sighandler_binary_arch",
    src = ":sighandler_binary",
    template = "sighandler.built-in.%s.bin",
)

sighandler_go(
    name = "sighandler_go",
    src = ":sighandler_object",
    out = "sighandler.go.arch",
)

arch_genrule(
    name = "sighandler_go_arch",
    src = ":sighandler_go",
    template = "sighandler_%s.go",
)

go_library(
    name = "sysmsg",
    srcs = [
        "sysmsg.go",
        "sysmsg_amd64.go",
        "sysmsg_arm64.go",
        "sysmsg_layout_amd64.go",
        "sysmsg_layout_arm64.go",
        "sysmsg_unsafe.go",
        ":sighandler_go_arch",
    ],
    embedsrcs = [
        ":sighandler_binary_arch",  # keep
    ],
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/abi/linux",
        "//pkg/cpuid",
        "//pkg/hostarch",
        "//pkg/hostsyscall",
        "//pkg/sentry/platform",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

build_test(
    name = "sysmsg_products_test",
    targets = [
        ":sighandler_binary",
        ":sighandler_go",
    ],
)
