load("//pkg/sync/locking:locking.bzl", "declare_mutex")
load("//tools:defs.bzl", "go_library", "go_test", "proto_library")

package(default_applicable_licenses = ["//:license"])

licenses(["notice"])

declare_mutex(
    name = "virtual_filesystem_mutex",
    out = "virtual_filesystem_mutex.go",
    package = "vfs",
    prefix = "virtualFilesystem",
)

declare_mutex(
    name = "inotify_event_mutex",
    out = "inotify_event_mutex.go",
    package = "vfs",
    prefix = "inotifyEvent",
)

declare_mutex(
    name = "inotify_mutex",
    out = "inotify_mutex.go",
    package = "vfs",
    prefix = "inotify",
)

declare_mutex(
    name = "epoll_instance_mutex",
    out = "epoll_instance_mutex.go",
    package = "vfs",
    prefix = "epollReadyInstance",
)

declare_mutex(
    name = "epoll_mutex",
    out = "epoll_mutex.go",
    package = "vfs",
    prefix = "epoll",
)

proto_library(
    name = "events",
    srcs = ["events.proto"],
    visibility = ["//visibility:public"],
)

go_library(
    name = "vfs",
    srcs = [
        "anonfs.go",
        "context.go",
        "debug_impl.go",
        "debug_testonly.go",
        "dentry.go",
        "device.go",
        "epoll.go",
        "epoll_instance_mutex.go",
        "epoll_mutex.go",
        "file_description.go",
        "file_description_impl_util.go",
        "filesystem.go",
        "filesystem_impl_util.go",
        "filesystem_type.go",
        "inotify.go",
        "inotify_event_mutex.go",
        "inotify_mutex.go",
        "lock.go",
        "mount.go",
        "mount_unsafe.go",
        "namespace.go",
        "opath.go",
        "options.go",
        "pathname.go",
        "permissions.go",
        "propagation.go",
        "resolving_path.go",
        "save_restore.go",
        "vfs.go",
        "virtual_filesystem_mutex.go",
    ],
    visibility = ["//pkg/sentry:internal"],
    deps = [
        ":events_go_proto",
        "//pkg/abi/linux",
        "//pkg/atomicbitops",
        "//pkg/bitmap",
        "//pkg/bits",
        "//pkg/cleanup",
        "//pkg/context",
        "//pkg/errors/linuxerr",
        "//pkg/eventchannel",
        "//pkg/fd",
        "//pkg/fdnotifier",
        "//pkg/fspath",
        "//pkg/gohacks",
        "//pkg/hostarch",
        "//pkg/ilist",
        "//pkg/ilist/ring",
        "//pkg/log",
        "//pkg/refs",
        "//pkg/safemem",
        "//pkg/sentry/arch",
        "//pkg/sentry/fsimpl/lock",
        "//pkg/sentry/fsmetric",
        "//pkg/sentry/kernel/auth",
        "//pkg/sentry/ktime",
        "//pkg/sentry/limits",
        "//pkg/sentry/memmap",
        "//pkg/sentry/socket/unix/transport",
        "//pkg/sentry/uniqueid",
        "//pkg/sync",
        "//pkg/sync/locking",
        "//pkg/usermem",
        "//pkg/waiter",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

go_test(
    name = "vfs_test",
    size = "small",
    srcs = [
        "file_description_impl_util_test.go",
        "mount_test.go",
    ],
    library = ":vfs",
    deps = [
        "//pkg/abi/linux",
        "//pkg/atomicbitops",
        "//pkg/context",
        "//pkg/errors/linuxerr",
        "//pkg/sentry/contexttest",
        "//pkg/sentry/kernel/auth",
        "//pkg/sync",
        "//pkg/usermem",
    ],
)
