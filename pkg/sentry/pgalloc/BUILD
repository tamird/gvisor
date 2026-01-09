load("//pkg/sync/locking:locking.bzl", "declare_mutex")
load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

declare_mutex(
    name = "amfls_mutex",
    out = "amfls_mutex.go",
    package = "pgalloc",
    prefix = "amfls",
)

declare_mutex(
    name = "apfl_mutex",
    out = "apfl_mutex.go",
    package = "pgalloc",
    prefix = "apfl",
)

declare_mutex(
    name = "apfs_mutex",
    out = "apfs_mutex.go",
    package = "pgalloc",
    prefix = "apfs",
)

declare_mutex(
    name = "memory_file_mutex",
    out = "memory_file_mutex.go",
    package = "pgalloc",
    prefix = "memoryFile",
)

go_library(
    name = "pgalloc",
    srcs = [
        "amfls_mutex.go",
        "apfl_mutex.go",
        "apfs_mutex.go",
        "context.go",
        "debug.go",
        "memory_file_mutex.go",
        "pgalloc.go",
        "pgalloc_unsafe.go",
        "save_restore.go",
    ],
    visibility = ["//pkg/sentry:internal"],
    deps = [
        "//pkg/abi/linux",
        "//pkg/atomicbitops",
        "//pkg/bitmap",
        "//pkg/context",
        "//pkg/errors/linuxerr",
        "//pkg/gohacks",
        "//pkg/goid",
        "//pkg/hostarch",
        "//pkg/ilist",
        "//pkg/log",
        "//pkg/ringdeque",
        "//pkg/safemem",
        "//pkg/segment",
        "//pkg/segment/set10",
        "//pkg/segment/set10gaps",
        "//pkg/sentry/arch",
        "//pkg/sentry/hostmm",
        "//pkg/sentry/memmap",
        "//pkg/sentry/state/stateio",
        "//pkg/sentry/usage",
        "//pkg/state",
        "//pkg/state/wire",
        "//pkg/sync",
        "//pkg/sync/locking",
        "//pkg/syncevent",
        "//pkg/timing",
        "//pkg/usermem",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

go_test(
    name = "pgalloc_test",
    size = "small",
    srcs = ["pgalloc_test.go"],
    library = ":pgalloc",
    deps = [
        "//pkg/hostarch",
        "//pkg/sentry/memmap",
    ],
)
