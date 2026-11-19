load("//tools:defs.bzl", "bzl_library", "go_binary", "go_test")
load("//tools:release.bzl", "RUNSC", "release_files", "sidecars")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

release_files(
    name = "coverage_release",
    testonly = True,
    bins = ["//shim:containerd-shim-runsc-v1"],
    runsc = select({
        "//tools:gotsan": RUNSC["race-coverage"],
        "//conditions:default": RUNSC["coverage"],
    }),
    sidecars = select({
        "//tools:gotsan": sidecars("race-coverage"),
        "//conditions:default": sidecars("coverage"),
    }),
    visibility = ["//:sandbox"],
)

_RUNNER_SRCS = [
    "coverage_report.go",
    "main.go",
    "xml.go",
]

_RUNNER_DEPS = [
    "//pkg/hostos",
    "//pkg/log",
    "//pkg/sentry/seccheck",
    "//pkg/state/statefile",
    "//pkg/test/testutil",
    "//runsc/specutils",
    "//test/runner/gtest",
    "//test/trace/config",
    "//test/uds",
    "@com_github_creack_pty//:go_default_library",
    "@com_github_moby_sys_capability//:go_default_library",
    "@com_github_opencontainers_runtime_spec//specs-go:go_default_library",
    "@org_golang_x_sys//unix:go_default_library",
]

go_binary(
    name = "runner",
    testonly = 1,
    srcs = _RUNNER_SRCS,
    data = [
        "//:release",
        "//test/runner/fuse",
        "//test/runner/setup_container",
    ],
    visibility = ["//:sandbox"],
    deps = _RUNNER_DEPS,
)

go_binary(
    name = "runner_coverage",
    testonly = 1,
    srcs = _RUNNER_SRCS,
    data = [
        ":coverage_release",
        "//test/runner/fuse",
        "//test/runner/setup_container",
    ],
    visibility = ["//:sandbox"],
    deps = _RUNNER_DEPS,
)

go_test(
    name = "runner_test",
    size = "small",
    srcs = [
        "coverage_report.go",
        "main.go",
        "main_test.go",
        "xml.go",
        "xml_test.go",
    ],
    data = ["//:release"],
    deps = [
        "//pkg/hostos",
        "//pkg/log",
        "//pkg/sentry/limits",
        "//pkg/sentry/seccheck",
        "//pkg/state",
        "//pkg/state/statefile",
        "//pkg/test/testutil",
        "//runsc/cli/sentrycli",
        "//runsc/specutils",
        "//test/runner/gtest",
        "//test/trace/config",
        "//test/uds",
        "@com_github_creack_pty//:go_default_library",
        "@com_github_moby_sys_capability//:go_default_library",
        "@com_github_opencontainers_runtime_spec//specs-go:go_default_library",
        "@org_golang_x_sys//unix:go_default_library",
    ],
)

go_test(
    name = "coverage_report_test",
    size = "small",
    srcs = [
        "coverage_report.go",
        "coverage_report_test.go",
    ],
)

# @unused
glaze_ignore = [
    "coverage_report.go",
    "coverage_report_test.go",
    "main.go",
    "main_test.go",
    "xml.go",
    "xml_test.go",
]

bzl_library(
    name = "defs_bzl",
    srcs = ["defs.bzl"],
    visibility = ["//visibility:private"],
    deps = [
        "//tools:defs_bzl",
        "//tools:release_bzl",
        "@bazel_skylib//lib:shell",
    ],
)
