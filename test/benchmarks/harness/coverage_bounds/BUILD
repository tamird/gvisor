load("//tools:defs.bzl", "go_binary")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_binary(
    name = "coverage_bounds",
    testonly = True,
    srcs = ["main.go"],
    args = [
        "$(rlocationpath //test/benchmarks/harness:startup-aa-base.txt)",
        "$(rlocationpath //test/benchmarks/harness:startup-aa-candidate.txt)",
    ],
    data = [
        "//test/benchmarks/harness:startup-aa-base.txt",
        "//test/benchmarks/harness:startup-aa-candidate.txt",
    ],
    pure = True,
    tags = ["manual"],
    deps = [
        "@io_bazel_rules_go//go/runfiles",
        "@org_golang_x_perf//benchfmt:go_default_library",
        "@org_golang_x_perf//benchmath:go_default_library",
    ],
)
