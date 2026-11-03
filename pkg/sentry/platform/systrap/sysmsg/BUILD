load("//tools:arch.bzl", "arch_genrule", "select_arch")
load("//tools:defs.bzl", "cc_flags_supplier", "cc_linker", "cc_nm", "cc_objcopy", "go_library")
load("build.bzl", "cc_pie_obj")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

cc_flags_supplier(
    name = "no_pie_cc_flags",
    features = ["-pie"],
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

genrule(
    name = "sighandler_object",
    srcs = [
        "pie.lds.S",
        ":sighandler",
        ":syshandler",
        ":sysrestorer",
        ":sysmsg_lib",
    ],
    outs = ["sighandler.built-in.bin.o"],
    cmd = "$(execpath {}) ".format(cc_linker) +
          "-pie -z noexecstack -T $(location pie.lds.S) " +
          "$(location :sysrestorer) $(location :sighandler) " +
          "$(location :syshandler) $(location :sysmsg_lib) " +
          "-o $@",
    features = ["-pie"],
    tools = [cc_linker],
)

genrule(
    name = "sighandler_binary",
    srcs = [":sighandler_object"],
    outs = ["sighandler.built-in.bin"],
    cmd = "$(execpath {}) -O binary $(SRCS) $@".format(cc_objcopy),
    tools = [cc_objcopy],
)

arch_genrule(
    name = "sighandler_binary_arch",
    src = ":sighandler_binary",
    template = "sighandler.built-in.%s.bin",
)

genrule(
    name = "sighandler_go",
    srcs = [":sighandler_object"],
    outs = ["sighandler.go.arch"],
    cmd = "bash -x $(location gen_offsets_go.sh) sighandler Sighandler " +
          "$(SRCS) \"$(execpath {})\" > $@".format(cc_nm),
    tools = [
        "gen_offsets_go.sh",
        cc_nm,
    ],
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
        "sysmsg_4k.go",
        "sysmsg_64k.go",
        "sysmsg_amd64.go",
        "sysmsg_arm64.go",
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
