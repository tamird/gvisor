load("@bazel_skylib//rules:native_binary.bzl", "native_binary")
load("//tools:defs.bzl", "bzl_library", "go_binary", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

native_binary(
    name = "coverage_archive_tar",
    src = "@bsd_tar_toolchains//:resolved_target_toolchain",
    testonly = True,
)

go_binary(
    name = "runner",
    testonly = 1,
    srcs = [
        "main.go",
        "xml.go",
    ],
    data = [
        ":coverage_archive_tar",
        "//debian:gvisor-release-coverage-tar-zstd",
        "//debian:gvisor-release-race-coverage-tar-zstd",
        "//test/runner/fuse",
        "//test/runner/setup_container",
    ],
    visibility = ["//:sandbox"],
    deps = [
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
    ],
)

go_test(
    name = "runner_test",
    size = "small",
    srcs = [
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

# @unused
glaze_ignore = [
    "main.go",
    "main_test.go",
    "xml.go",
    "xml_test.go",
]

bzl_library(
    name = "defs_bzl",
    srcs = ["defs.bzl"],
    visibility = ["//visibility:private"],
    deps = ["//tools:defs_bzl"],
)
