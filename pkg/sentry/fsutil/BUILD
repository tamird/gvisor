load("//pkg/sync/locking:locking.bzl", "declare_mutex")
load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

declare_mutex(
    name = "refs_mutex",
    out = "refs_mutex.go",
    package = "fsutil",
    prefix = "refs",
)

declare_mutex(
    name = "maps_mutex",
    out = "maps_mutex.go",
    package = "fsutil",
    prefix = "maps",
)

go_library(
    name = "fsutil",
    srcs = [
        "dirty_set.go",
        "file_range_set.go",
        "frame_ref_set.go",
        "fsutil.go",
        "host_file_mapper.go",
        "host_file_mapper_state.go",
        "host_file_mapper_unsafe.go",
        "maps_mutex.go",
        "precise_host_file_mapper.go",
        "refs_mutex.go",
    ],
    visibility = ["//pkg/sentry:internal"],
    deps = [
        "//pkg/abi/linux",
        "//pkg/context",
        "//pkg/errors/linuxerr",
        "//pkg/hostarch",
        "//pkg/log",
        "//pkg/safemem",
        "//pkg/sentry/arch",
        "//pkg/sentry/ktime",
        "//pkg/sentry/memmap",
        "//pkg/sentry/mm",
        "//pkg/sentry/pgalloc",
        "//pkg/sentry/usage",
        "//pkg/state",
        "//pkg/sync",
        "//pkg/sync/locking",
        "//pkg/usermem",
        "//pkg/waiter",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

go_test(
    name = "fsutil_test",
    size = "small",
    srcs = [
        "dirty_set_test.go",
    ],
    library = ":fsutil",
    deps = [
        "//pkg/hostarch",
        "//pkg/sentry/memmap",
    ],
)
