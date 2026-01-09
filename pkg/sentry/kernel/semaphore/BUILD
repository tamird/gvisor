load("//tools:defs.bzl", "go_library", "go_test")
load("//tools/go_generics:defs.bzl", "go_template_instance")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "semaphore",
    srcs = [
        "semaphore.go",
    ],
    visibility = ["//pkg/sentry:internal"],
    deps = [
        "//pkg/abi/linux",
        "//pkg/context",
        "//pkg/errors/linuxerr",
        "//pkg/ilist",
        "//pkg/sentry/kernel/auth",
        "//pkg/sentry/kernel/ipc",
        "//pkg/sentry/ktime",
        "//pkg/sentry/vfs",
        "//pkg/sync",
    ],
)

go_test(
    name = "semaphore_test",
    size = "small",
    srcs = ["semaphore_test.go"],
    library = ":semaphore",
    deps = [
        "//pkg/abi/linux",  # keep
        "//pkg/context",  # keep
        "//pkg/errors/linuxerr",  #keep
        "//pkg/sentry/contexttest",  # keep
        "//pkg/sentry/kernel/auth",  # keep
        "//pkg/sentry/kernel/ipc",  # keep
    ],
)
