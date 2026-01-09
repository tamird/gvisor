load("//tools:defs.bzl", "go_library")

package(default_applicable_licenses = ["//:license"])

licenses(["notice"])

go_library(
    name = "accel",
    srcs = [
        "accel.go",
        "accel_fd.go",
        "accel_fd_mmap.go",
        "gasket_ioctl.go",
    ],
    visibility = ["//pkg/sentry:internal"],
    deps = [
        "//pkg/abi/gasket",
        "//pkg/abi/linux",
        "//pkg/abi/tpu",
        "//pkg/cleanup",
        "//pkg/context",
        "//pkg/devutil",
        "//pkg/errors/linuxerr",
        "//pkg/fdnotifier",
        "//pkg/hostarch",
        "//pkg/log",
        "//pkg/segment",
        "//pkg/safemem",
        "//pkg/seccomp",
        "//pkg/sentry/arch",
        "//pkg/sentry/devices/tpuproxy/util",
        "//pkg/sentry/fsimpl/eventfd",
        "//pkg/sentry/kernel",
        "//pkg/sentry/kernel/auth",
        "//pkg/sentry/memmap",
        "//pkg/sentry/mm",
        "//pkg/sentry/vfs",
        "//pkg/sync",
        "//pkg/usermem",
        "//pkg/waiter",
        "@org_golang_x_exp//constraints:go_default_library",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)
