load("//pkg/sync/locking:locking.bzl", "declare_mutex", "declare_rwmutex")
load("//tools:defs.bzl", "go_library", "go_test")

package(default_applicable_licenses = ["//:license"])

licenses(["notice"])

declare_rwmutex(
    name = "ancestry_mutex",
    out = "ancestry_mutex.go",
    package = "tmpfs",
    prefix = "ancestry",
)

declare_rwmutex(
    name = "filesystem_mutex",
    out = "filesystem_mutex.go",
    package = "tmpfs",
    prefix = "filesystem",
)

declare_mutex(
    name = "inode_mutex",
    out = "inode_mutex.go",
    package = "tmpfs",
    prefix = "inode",
)

declare_mutex(
    name = "iter_mutex",
    out = "iter_mutex.go",
    package = "tmpfs",
    prefix = "iter",
)

declare_mutex(
    name = "pages_used_mutex",
    out = "pages_used_mutex.go",
    package = "tmpfs",
    prefix = "pagesUsed",
)

go_library(
    name = "tmpfs",
    srcs = [
        "ancestry_mutex.go",
        "device_file.go",
        "directory.go",
        "filesystem.go",
        "filesystem_mutex.go",
        "inode_mutex.go",
        "iter_mutex.go",
        "named_pipe.go",
        "pages_used_mutex.go",
        "regular_file.go",
        "save_restore.go",
        "socket_file.go",
        "symlink.go",
        "tar.go",
        "tmpfs.go",
    ],
    visibility = ["//pkg/sentry:internal"],
    deps = [
        "//pkg/abi/linux",
        "//pkg/atomicbitops",
        "//pkg/context",
        "//pkg/errors/linuxerr",
        "//pkg/fd",
        "//pkg/fspath",
        "//pkg/fsutil",
        "//pkg/hostarch",
        "//pkg/ilist",
        "//pkg/log",
        "//pkg/refs",
        "//pkg/safemem",
        "//pkg/sentry/arch",
        "//pkg/sentry/fsimpl/lock",
        "//pkg/sentry/fsmetric",
        "//pkg/sentry/fsutil",
        "//pkg/sentry/hostfd",
        "//pkg/sentry/kernel/auth",
        "//pkg/sentry/kernel/pipe",
        "//pkg/sentry/ktime",
        "//pkg/sentry/memmap",
        "//pkg/sentry/pgalloc",
        "//pkg/sentry/platform",
        "//pkg/sentry/socket/unix/transport",
        "//pkg/sentry/uniqueid",
        "//pkg/sentry/usage",
        "//pkg/sentry/vfs",
        "//pkg/sentry/vfs/genericfstree",
        "//pkg/sentry/vfs/memxattr",
        "//pkg/sync",
        "//pkg/sync/locking",
        "//pkg/usermem",
    ],
)

go_test(
    name = "benchmark_test",
    size = "small",
    srcs = ["benchmark_test.go"],
    deps = [
        ":tmpfs",
        "//pkg/abi/linux",
        "//pkg/fspath",
        "//pkg/refs",
        "//pkg/sentry/contexttest",
        "//pkg/sentry/kernel/auth",
        "//pkg/sentry/vfs",
    ],
)

go_test(
    name = "tmpfs_test",
    size = "small",
    srcs = [
        "pipe_test.go",
        "regular_file_test.go",
        "stat_test.go",
        "tmpfs_test.go",
    ],
    library = ":tmpfs",
    deps = [
        "//pkg/abi/linux",
        "//pkg/atomicbitops",
        "//pkg/context",
        "//pkg/errors/linuxerr",
        "//pkg/fspath",
        "//pkg/sentry/contexttest",
        "//pkg/sentry/fsimpl/lock",
        "//pkg/sentry/kernel/auth",
        "//pkg/sentry/vfs",
        "//pkg/usermem",
    ],
)
