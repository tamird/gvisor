load("@rules_shell//shell:sh_test.bzl", "sh_test")
load("@bazel_skylib//rules:run_binary.bzl", "run_binary")
load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "secfuzz",
    testonly = 1,
    srcs = [
        "secfuzz.go",
        "secfuzz_covermeup.go",
        "secfuzz_nocoverageguidance.go",
    ],
    importpath = "gvisor.dev/gvisor/test/secfuzz",
    visibility = [
        "//:sandbox",
    ],
    deps = [
        "//pkg/abi/linux",
        "//pkg/abi/sentry",
        "//pkg/atomicbitops",
        "//pkg/bpf",
        "//pkg/seccomp",
        "//pkg/sync",
    ],
)

go_test(
    name = "secfuzz_test",
    size = "small",
    srcs = ["coverage_test.go"],
    embed = [":secfuzz"],
    deps = ["//pkg/bpf"],
)

go_library(
    name = "instrumented",
    testonly = 1,
    srcs = ["instrumentation.go"],
    embed = [":secfuzz"],
    gc_goopts = ["-d=libfuzzer"],
    stateify = False,
    deps = ["//pkg/bpf"],
)

go_test(
    name = "fuzz_instrumentation_test",
    size = "small",
    srcs = ["fuzz_instrumentation_test.go"],
    # The pinned Go fuzzing engine has no exported counter reader. Limit its
    # private-symbol reference to this test binary.
    gc_linkopts = ["-checklinkname=0"],
    gotags = ["fuzz"],
    pure = True,
    target_compatible_with = ["@platforms//os:linux"] + select({
        "@platforms//cpu:aarch64": [],
        "@platforms//cpu:x86_64": [],
        "//conditions:default": ["@platforms//:incompatible"],
    }),
    deps = [
        ":instrumented",
        "//pkg/bpf",
    ],
)

run_binary(
    name = "secfuzz_covermeup",
    srcs = ["secfuzz_covermeup.tmpl"],
    outs = ["secfuzz_covermeup.go"],
    args = [
        "$(location secfuzz_covermeup.tmpl)",
        "$(location secfuzz_covermeup.go)",
    ],
    tool = "//test/secfuzz/coveragegen",
)

sh_test(
    name = "instrumentation_format_test",
    srcs = ["instrumentation_format_test.sh"],
    args = [
        "$(rootpath @org_golang_x_tools//cmd/goimports)",
        "$(rootpath @com_github_bazelbuild_buildtools//buildifier)",
        "$(rootpath instrumentation.go)",
        "$(rootpath fuzz_instrumentation_test.go)",
        "$(rootpath BUILD)",
    ],
    data = [
        "BUILD",
        "fuzz_instrumentation_test.go",
        "instrumentation.go",
        "@com_github_bazelbuild_buildtools//buildifier",
        "@org_golang_x_tools//cmd/goimports",
    ],
    target_compatible_with = ["@platforms//os:linux"],
)
