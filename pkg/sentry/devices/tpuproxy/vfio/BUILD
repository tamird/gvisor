load("//tools:defs.bzl", "go_library")

package(default_applicable_licenses = ["//:license"])

licenses(["notice"])

go_library(
    name = "vfio",
    srcs = [
        "pci_device_fd.go",
        "pci_device_fd_mmap.go",
        "save_restore.go",
        "tpu_fd.go",
        "tpu_fd_mmap.go",
        "vfio.go",
        "vfio_fd.go",
        "vfio_fd_mmap.go",
    ],
    visibility = [
        "//pkg/sentry:internal",
    ],
    deps = [
        "//pkg/abi/linux",
        "//pkg/cleanup",
        "//pkg/context",
        "//pkg/devutil",
        "//pkg/errors/linuxerr",
        "//pkg/fdnotifier",
        "//pkg/hostarch",
        "//pkg/log",
        "//pkg/marshal/primitive",
        "//pkg/safemem",
        "//pkg/sentry/arch",
        "//pkg/sentry/devices/tpuproxy/util",
        "//pkg/sentry/fsimpl/eventfd",
        "//pkg/sentry/fsutil",
        "//pkg/sentry/kernel",
        "//pkg/sentry/kernel/auth",
        "//pkg/sentry/memmap",
        "//pkg/sentry/mm",
        "//pkg/sentry/vfs",
        "//pkg/segment",
        "//pkg/sync",
        "//pkg/usermem",
        "//pkg/waiter",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)
