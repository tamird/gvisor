load("//tools:defs.bzl", "cc_binary", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

# Tell build_cleaner not to create an unnecessary cc_library for
# procumounter.c.
filegroup(
    name = "ignore_srcs",
    srcs = ["procumounter.c"],
    tags = ["ignore_srcs"],
)

# gvisor-proc-umounter must remain a statically-linked, freestanding (no libc)
# C binary that fits in a single 4KiB page. Read its comments as to why.
cc_binary(
    name = "gvisor-proc-umounter",
    srcs = ["procumounter.c"],
    copts = [
        "-Os",
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
    ],
    link_extra_lib = None,
    linkopts = [
        "-static",
        "-nostdlib",
        "-nostartfiles",
        "-Wl,--build-id=none",
        "-Wl,--gc-sections",
        "-Wl,-s",
        "-Wl,-n",
    ],
    linkstatic = True,
    stamp = 0,
    visibility = ["//visibility:public"],
)

alias(
    name = "procumounter_binary",
    actual = ":gvisor-proc-umounter",
    visibility = ["//visibility:public"],
)

go_test(
    name = "procumounter_test",
    size = "small",
    srcs = ["procumounter_test.go"],
    data = [":procumounter_binary"],
    deps = [
        "//pkg/test/testutil",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)
