load("//tools:defs.bzl", "go_test")
load("//tools/go_generics/tests:defs.bzl", "go_generics_test")

package(default_applicable_licenses = ["//:license"])

go_generics_test(
    name = "typeparams",
    inputs = ["input.go"],
    output = "output.go",
    package = "tests",
    types = {
        "T": "MyT",
    },
)

go_test(
    name = "typeparams_compile_test",
    size = "small",
    srcs = [
        "typeparams_output.go",
        "typeparams_test.go",
    ],
    deps = ["//tools/go_generics/tests/typeparams/callbacks"],
)

# @unused
glaze_ignore = [
    "input.go",
    "output.go",
]
