load("//pkg/sync/locking:locking.bzl", "declare_mutex", "declare_rwmutex")
load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

declare_mutex(
    name = "user_namespace_mutex",
    out = "user_namespace_mutex.go",
    nested_lock_names = ["ns"],
    package = "auth",
    prefix = "userNamespace",
)

declare_rwmutex(
    name = "keyset_mutex",
    out = "keyset_mutex.go",
    package = "auth",
    prefix = "keyset",
)

declare_mutex(
    name = "keyset_transaction_mutex",
    out = "keyset_transaction_mutex.go",
    package = "auth",
    prefix = "keysetTransaction",
)

go_library(
    name = "auth",
    srcs = [
        "auth.go",
        "capability_set.go",
        "context.go",
        "credentials.go",
        "id.go",
        "id_map.go",
        "id_map_functions.go",
        "key.go",
        "keyset_mutex.go",
        "keyset_transaction_mutex.go",
        "user_namespace.go",
        "user_namespace_mutex.go",
    ],
    marshal = True,
    visibility = [
        "//pkg/sentry:internal",
        "//test/e2e:__subpackages__",
    ],
    deps = [
        "//pkg/abi/linux",
        "//pkg/bits",
        "//pkg/context",
        "//pkg/errors/linuxerr",
        "//pkg/log",
        "//pkg/rand",
        "//pkg/sentry/seccheck",
        "//pkg/sentry/seccheck/points:points_go_proto",
        "//pkg/segment",
        "//pkg/sync",
        "//pkg/sync/atomicptr",
        "//pkg/sync/locking",
    ],
)

go_test(
    name = "auth_test",
    srcs = ["capability_set_test.go"],
    library = ":auth",
    deps = [
        "//pkg/abi/linux",
        "//pkg/errors/linuxerr",
    ],
)
