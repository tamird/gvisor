load("@bazel_skylib//rules:native_binary.bzl", "native_test")
load("//tools:defs.bzl", "go_cov")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_cov(
    name = "probe",
    testonly = True,
    srcs = ["main.go"],
    out = "probe_cov",
    gotags = [
        "kcov",
        "opensource",
    ],
    pure = True,
    race = "off",
    deps = [
        "//pkg/coverage",
        "//tools/bazeldefs/tests/coverage:own",
    ],
)

native_test(
    name = "protobuf_coverage_test",
    src = ":probe",
    args = ["42"],
)
