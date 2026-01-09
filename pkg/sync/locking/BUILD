load("//pkg/sync/locking:locking.bzl", "declare_mutex", "declare_rwmutex")
load("//tools:defs.bzl", "go_library", "go_test")
load("//tools/go_generics:defs.bzl", "go_template")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//:sandbox"],
    licenses = ["notice"],
)

go_library(
    name = "locking",
    srcs = [
        "lockdep.go",
        "lockdep_norace.go",
        "locking.go",
    ],
    marshal = False,
    stateify = False,
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/gohacks",
        "//pkg/goid",
        "//pkg/log",
        "//pkg/sync",
        "//pkg/sync/atomicptrmap",
    ],
)

go_template(
    name = "generic_mutex",
    srcs = ["generic_mutex.go"],
    visibility = ["//:sandbox"],
)

go_template(
    name = "generic_rwmutex",
    srcs = ["generic_rwmutex.go"],
    visibility = ["//:sandbox"],
)

declare_mutex(
    name = "mutex_test",
    out = "mutex_test.go",
    nested_lock_names = [
        "m2",
        "m3",
    ],
    package = "locking_test",
    prefix = "test",
)

declare_rwmutex(
    name = "mutex_test2",
    out = "mutex_test2.go",
    package = "locking_test",
    prefix = "test2",
)

declare_mutex(
    name = "mutex_test3",
    out = "mutex_test3.go",
    package = "locking_test",
    prefix = "test3",
)

go_test(
    name = "locking_test",
    size = "small",
    srcs = [
        "lockdep_nolockdep_test.go",
        "lockdep_test.go",
        "mutex_test.go",
        "mutex_test2.go",
        "mutex_test3.go",
    ],
    deps = [
        "//pkg/sync",
        "//pkg/sync/locking",
    ],
)
