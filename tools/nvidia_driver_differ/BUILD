load("//tools:defs.bzl", "cc_binary", "go_binary", "go_test")
load(":defs.bzl", "driver_abi")
load(":driver_sources.bzl", "DRIVER_SOURCES")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

exports_files(["driver_source.BUILD"])

driver_abi(
    name = "driver_abi",
    sources = DRIVER_SOURCES,
    visibility = ["//pkg/sentry/devices/nvproxy:__pkg__"],
)

cc_binary(
    name = "driver_ast_parser",
    srcs = [
        "driver_ast_parser.cc",
        "driver_ast_parser.h",
    ],
    visibility = [
        "//pkg/sentry/devices/nvproxy:__subpackages__",
    ],
    deps = [
        "@com_google_absl//absl/container:flat_hash_set",
        "@com_google_absl//absl/strings",
        "@llvm-project//clang:ast",
        "@llvm-project//clang:ast_matchers",
        "@llvm-project//clang:tooling",
        "@llvm-project//llvm:Support",
        "@nlohmann_json//:json",
    ],
)

go_test(
    name = "driver_ast_parser_test",
    srcs = ["driver_ast_parser_test.go"],
    data = [
        "test_struct.cc",
        ":driver_ast_parser",
    ],
    deps = [
        "//pkg/test/testutil",
        "//tools/nvidia_driver_differ/parser",
        "@com_github_google_go_cmp//cmp:go_default_library",
        "@com_github_google_go_cmp//cmp/cmpopts:go_default_library",
    ],
)

go_binary(
    name = "run_differ",
    srcs = ["run_differ.go"],
    embedsrcs = [
        ":driver_ast_parser",  # keep
    ],
    # Disable nogo tests because this has C++ dependencies.
    nogo = False,
    deps = [
        "//pkg/log",
        "//pkg/sentry/devices/nvproxy",
        "//pkg/sentry/devices/nvproxy/nvconf",
        "//tools/nvidia_driver_differ/parser",
    ],
)
