load("//tools:defs.bzl", "go_library", "go_test")
load("//tools/go_generics:defs.bzl", "go_template_instance")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_template_instance(
    name = "synthetic_timer_set",
    out = "synthetic_timer_set.go",
    package = "ktime",
    prefix = "syntheticTimer",
    template = "//pkg/segment:generic_set",
    types = {
        "Key": "uint64",
        "Range": "uint64Range",
        "Value": "syntheticTimerQueue",
        "Functions": "syntheticTimerSetFunctions",
    },
)

go_library(
    name = "ktime",
    srcs = [
        "context.go",
        "ktime.go",
        "sampled_timer.go",
        "synthetic_timer.go",
        "synthetic_timer_set.go",
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
