load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "ktime",
    srcs = [
        "context.go",
        "ktime.go",
        "sampled_timer.go",
        "synthetic_timer.go",
    ],
    visibility = ["//pkg/sentry:internal"],
    deps = [
        "//pkg/abi/linux",
        "//pkg/atomicbitops",
        "//pkg/context",
        "//pkg/errors/linuxerr",
        "//pkg/gohacks",
        "//pkg/ilist",
        "//pkg/segment",
        "//pkg/sync",
        "//pkg/sync/seqatomic",
        "//pkg/waiter",
    ],
)

go_test(
    name = "ktime_test",
    size = "small",
    srcs = [
        "synthetic_timer_test.go",
    ],
    library = ":ktime",
)
