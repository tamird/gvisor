load("//tools:defs.bzl", "go_binary", "go_test")

package(default_applicable_licenses = ["//:license"])

DEPS = [
    "@org_golang_x_perf//benchfmt:go_default_library",
    "@org_golang_x_perf//benchmath:go_default_library",
]

go_binary(
    name = "paired",
    srcs = ["main.go"],
    visibility = ["//visibility:public"],
    deps = DEPS,
)

go_test(
    name = "paired_test",
    srcs = [
        "main.go",
        "main_test.go",
    ],
    deps = DEPS,
)
