load("//tools:defs.bzl", "cc_binary", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

# Tell build_cleaner not to create an unnecessary cc_library for
# fdparking.c.
filegroup(
    name = "ignore_srcs",
    srcs = ["fdparking.c"],
    tags = ["ignore_srcs"],
)

# runsc-fd-parking must remain a statically-linked, freestanding
# (no libc) C binary. It exists for the lifetime of each sandbox just to hold
# some FDs, so its runtime memory footprint *must* stay absolutely minimal.
cc_binary(
    name = "runsc-fd-parking",
    srcs = ["fdparking.c"],
    copts = [
        "-Os",
        "-fno-pie",
        "-ffreestanding",
        "-fno-sanitize=all",
        "-fno-stack-protector",
        "-fno-asynchronous-unwind-tables",
        "-fno-unwind-tables",
    ],
    features = [
        "-coverage",
        "-default_compile_flags",
        "-default_link_flags",
        "-pie",
        "-unfiltered_compile_flags",
    ],
    link_extra_lib = None,
    linkopts = [
        "-static",
        "-nostdlib",
        "-nostartfiles",
        "-Wl,--build-id=none",
        "-Wl,--gc-sections",
        "-Wl,-n",
        "-Wl,-s",
    ],
    linkstatic = True,
    stamp = 0,
    visibility = ["//visibility:public"],
)

alias(
    name = "fdparking_binary",
    actual = ":runsc-fd-parking",
    visibility = ["//visibility:public"],
)

go_test(
    name = "fdparking_test",
    size = "small",
    srcs = ["fdparking_test.go"],
    data = [":fdparking_binary"],
    deps = [
        "//pkg/abi/linux",
        "//pkg/test/testutil",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)
