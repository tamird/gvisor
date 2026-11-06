load("//pkg/sync/locking:locking.bzl", "declare_mutex")
load("//tools:defs.bzl", "go_library", "go_test")
load("//tools/go_generics:defs.bzl", "go_template_instance")
load("//tools/nvidia_driver_differ:driver_sources.bzl", "DRIVER_SOURCES")

package(default_applicable_licenses = ["//:license"])

licenses(["notice"])

declare_mutex(
    name = "fds_mutex",
    out = "fds_mutex.go",
    package = "nvproxy",
    prefix = "fds",
)

declare_mutex(
    name = "frontend_mmap_mutex",
    out = "frontend_mmap_mutex.go",
    package = "nvproxy",
    prefix = "frontendMmap",
)

declare_mutex(
    name = "objs_mutex",
    out = "objs_mutex.go",
    package = "nvproxy",
    prefix = "objs",
)

go_template_instance(
    name = "object_free_list",
    out = "object_free_list.go",
    package = "nvproxy",
    prefix = "objectFree",
    template = "//pkg/ilist:generic_list",
    types = {
        "Element": "*object",
        "Linker": "*object",
    },
)

go_library(
    name = "nvproxy",
    srcs = [
        "fds_mutex.go",
        "frontend.go",
        "frontend_mmap.go",
        "frontend_mmap_mutex.go",
        "frontend_mmap_unsafe.go",
        "frontend_unsafe.go",
        "handlers.go",
        "nvproxy.go",
        "nvproxy_unsafe.go",
        "object.go",
        "object_free_list.go",
        "objs_mutex.go",
        "open_only.go",
        "procfs.go",
        "save_restore.go",
        "save_restore_unsafe.go",
        "seccomp_filters.go",
        "uvm.go",
        "uvm_mmap.go",
        "uvm_tools.go",
        "uvm_unsafe.go",
        "version.go",
    ],
    visibility = [
        "//pkg/sentry:internal",
        "//test/gpu:__subpackages__",
        "//tools:__subpackages__",
    ],
    deps = [
        "//pkg/abi/linux",
        "//pkg/abi/nvgpu",
        "//pkg/atomicbitops",
        "//pkg/cleanup",
        "//pkg/context",
        "//pkg/devutil",
        "//pkg/errors/linuxerr",
        "//pkg/fdnotifier",
        "//pkg/hostarch",
        "//pkg/log",
        "//pkg/marshal",
        "//pkg/marshal/primitive",
        "//pkg/safemem",
        "//pkg/seccomp",
        "//pkg/sentry/arch",
        "//pkg/sentry/devices/nvproxy/nvconf",
        "//pkg/sentry/fsutil",
        "//pkg/sentry/kernel",
        "//pkg/sentry/kernel/auth",
        "//pkg/sentry/memmap",
        "//pkg/sentry/mm",
        "//pkg/sentry/vfs",
        "//pkg/sync",
        "//pkg/sync/locking",
        "//pkg/usermem",
        "//pkg/waiter",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

go_test(
    name = "nvproxy_test",
    srcs = ["nvproxy_test.go"],
    embed = [":nvproxy"],
    deps = [
        "//pkg/abi/nvgpu",
        "//pkg/seccomp",
        "//pkg/sentry/devices/nvproxy/nvconf",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

go_test(
    name = "nvproxy_driver_abi_test",
    size = "large",
    srcs = ["nvproxy_driver_parity_test.go"],
    data = ["//tools/nvidia_driver_differ:driver_ast_parser"] + [
        repo + label
        for repo in DRIVER_SOURCES.values()
        if repo
        for label in ["//:COPYING", "//:sources"]
    ],
    env = {
        "NVIDIA_DRIVER_SOURCES": json.encode({
            version: "$(rootpath %s//:COPYING)" % repo if repo else None
            for version, repo in DRIVER_SOURCES.items()
        }),
    },
    deps = [
        ":nvproxy",
        "//pkg/abi/nvgpu",
        "//pkg/sentry/devices/nvproxy/nvconf",
        "//pkg/test/testutil",
        "//tools/nvidia_driver_differ/parser",
    ],
)

go_test(
    name = "nvproxy_driver_checksums_test",
    size = "large",
    srcs = ["nvproxy_driver_checksums_test.go"],
    data = ["@com_github_grpc_grpc//:root_certificates"],
    # The download uses HTTPS even on workers without a system CA store.
    env = {"SSL_CERT_FILE": "$(rootpath @com_github_grpc_grpc//:root_certificates)"},
    rundir = ".",
    # Verify the current upstream installers on every invocation.
    tags = [
        "external",
        "requires-network",
    ],
    deps = [
        ":nvproxy",
        "//pkg/sentry/devices/nvproxy/nvconf",
        "//tools/gpu/drivers",
    ],
)

test_suite(
    name = "nvproxy_driver_parity_test",
    tests = [
        ":nvproxy_driver_abi_test",
        ":nvproxy_driver_checksums_test",
    ],
)
