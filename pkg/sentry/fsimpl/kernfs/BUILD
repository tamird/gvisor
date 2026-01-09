load("//pkg/sync/locking:locking.bzl", "declare_mutex", "declare_rwmutex")
load("//tools:defs.bzl", "go_library", "go_test")

package(default_applicable_licenses = ["//:license"])

licenses(["notice"])

declare_rwmutex(
    name = "ancestry_mutex",
    out = "ancestry_mutex.go",
    package = "kernfs",
    prefix = "ancestry",
)

declare_mutex(
    name = "deferred_dec_refs_mutex",
    out = "deferred_dec_refs_mutex.go",
    package = "kernfs",
    prefix = "deferredDecRefs",
)

declare_rwmutex(
    name = "filesystem_mutex",
    out = "filesystem_mutex.go",
    package = "kernfs",
    prefix = "filesystem",
)

go_library(
    name = "kernfs",
    srcs = [
        "ancestry_mutex.go",
        "deferred_dec_refs_mutex.go",
        "dynamic_bytes_file.go",
        "fd_impl_util.go",
        "filesystem.go",
        "filesystem_mutex.go",
        "inode_impl_util.go",
        "kernfs.go",
        "mmap_util.go",
        "save_restore.go",
        "symlink.go",
        "synthetic_directory.go",
    ],
    visibility = ["//pkg/sentry:internal"],
    deps = [
        "//pkg/abi/linux",
        "//pkg/atomicbitops",
        "//pkg/context",
        "//pkg/errors/linuxerr",
        "//pkg/fspath",
        "//pkg/hostarch",
        "//pkg/ilist",
        "//pkg/log",
        "//pkg/refs",
        "//pkg/safemem",
        "//pkg/sentry/fsimpl/lock",
        "//pkg/sentry/fsutil",
        "//pkg/sentry/kernel/auth",
        "//pkg/sentry/ktime",
        "//pkg/sentry/memmap",
        "//pkg/sentry/socket/unix/transport",
        "//pkg/sentry/vfs",
        "//pkg/sentry/vfs/genericfstree",
        "//pkg/sync",
        "//pkg/sync/locking",
        "//pkg/usermem",
    ],
)

go_test(
    name = "kernfs_test",
    size = "small",
    srcs = [
        "kernfs_test.go",
    ],
    deps = [
        ":kernfs",
        "//pkg/abi/linux",
        "//pkg/atomicbitops",
        "//pkg/context",
        "//pkg/errors/linuxerr",
        "//pkg/fspath",
        "//pkg/log",
        "//pkg/refs",
        "//pkg/sentry/contexttest",
        "//pkg/sentry/fsimpl/testutil",
        "//pkg/sentry/kernel/auth",
        "//pkg/sentry/vfs",
        "//pkg/usermem",
        "@com_github_google_go_cmp//cmp:go_default_library",
    ],
)
