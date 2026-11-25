load("//tools:defs.bzl", "go_binary", "go_test")

package(default_applicable_licenses = ["//:license"])

DEPS = [
    "@org_golang_x_perf//benchfmt:go_default_library",
] + select({
    "@platforms//os:linux": ["@org_golang_x_sys//unix:go_default_library"],
    "//conditions:default": [],
})

SRCS = [
    "affinity_linux.go",
    "affinity_other.go",
    "compare.go",
    "main.go",
]

go_binary(
    name = "paired",
    srcs = SRCS,
    visibility = ["//visibility:public"],
    deps = DEPS,
)

go_test(
    name = "paired_test",
    srcs = SRCS + ["main_test.go"],
    data = ["@org_golang_x_perf//cmd/benchstat:benchstat"],
    env = {"PAIRED_BENCHSTAT": "$(rootpath @org_golang_x_perf//cmd/benchstat:benchstat)"},
    deps = DEPS + ["//pkg/test/testutil"],
)

exports_files(["test.sh"])
