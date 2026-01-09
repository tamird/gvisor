load("//pkg/sync/locking:locking.bzl", "declare_mutex")
load("//tools:defs.bzl", "go_library", "go_test")
load("//tools/go_generics:defs.bzl", "go_template_instance")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

declare_mutex(
    name = "futex_mutex",
    out = "futex_mutex.go",
    nested_lock_names = [
        "b",
    ],
    package = "futex",
    prefix = "futexBucket",
)

go_library(
    name = "futex",
    srcs = [
        "futex.go",
        "futex_mutex.go",
    ],
    visibility = ["//pkg/sentry:internal"],
    deps = [
        "//pkg/abi/linux",
        "//pkg/context",
        "//pkg/errors/linuxerr",
        "//pkg/hostarch",
        "//pkg/ilist",
        "//pkg/log",
        "//pkg/sentry/memmap",
        "//pkg/sync",
        "//pkg/sync/atomicptr",
        "//pkg/sync/locking",
        "//pkg/usermem",
    ],
)

go_test(
    name = "futex_test",
    size = "small",
    srcs = ["futex_test.go"],
    library = ":futex",
    deps = [
        "//pkg/atomicbitops",
        "//pkg/context",
        "//pkg/errors/linuxerr",
        "//pkg/hostarch",
        "//pkg/sync",
    ],
)
