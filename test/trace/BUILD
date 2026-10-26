load("//tools:defs.bzl", "go_library", "go_test", "namespace_test_exec_properties")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_test(
    name = "trace_test",
    srcs = [
        "trace_amd64_test.go",
        "trace_arm64_test.go",
        "trace_test.go",
    ],
    data = [
        "//:release",
        "//test/trace/workload",
    ],
    embed = [":trace"],
    # runsc do must acquire capabilities in a new user namespace.
    exec_properties = namespace_test_exec_properties(user = "nobody"),
    visibility = ["//test/rbe:__pkg__"],
    deps = [
        "//pkg/sentry/seccheck",
        "//pkg/sentry/seccheck/points:points_go_proto",
        "//pkg/sentry/seccheck/sinks/remote/test",
        "//pkg/test/testutil",
        "//test/trace/config",
        "@org_golang_google_protobuf//proto:go_default_library",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

go_library(
    name = "trace",
    srcs = ["trace.go"],
)
