load("//tools:defs.bzl", "go_library", "go_test")

package(default_applicable_licenses = ["//:license"])

licenses(["notice"])

go_library(
    name = "seccheck",
    srcs = [
        "config.go",
        "metadata.go",
        "metadata_amd64.go",
        "metadata_arm64.go",
        "seccheck.go",
        "syscall.go",
    ],
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/abi",
        "//pkg/abi/linux",
        "//pkg/abi/sentry",
        "//pkg/atomicbitops",
        "//pkg/context",
        "//pkg/fd",
        "//pkg/gohacks",
        "//pkg/log",
        "//pkg/metric",
        "//pkg/sentry/arch",
        "//pkg/sentry/ktime",
        "//pkg/sentry/seccheck/points:points_go_proto",
        "//pkg/sync",
        "//pkg/sync/seqatomic",
        "@org_golang_google_protobuf//proto:go_default_library",
    ],
)

go_test(
    name = "seccheck_test",
    size = "small",
    srcs = [
        "config_test.go",
        "metadata_test.go",
        "seccheck_test.go",
    ],
    library = ":seccheck",
    deps = [
        "//pkg/context",
        "//pkg/fd",
        "//pkg/sentry/seccheck/points:points_go_proto",
    ],
)
