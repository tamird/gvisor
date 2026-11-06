load("@io_bazel_rules_go//go:def.bzl", "go_binary")
load("//test/rbe/release_prewarmer:native.bzl", "native_release")

go_binary(
    name = "probe",
    srcs = ["main.go"],
    deps = ["//pkg/atomicbitops"],
)

# The original release transition forwards its three outputs unchanged.
native_release(
    name = "paired",
    arch = "arm64",
    archive = ":probe",
    runsc = ":probe",
    sentry = ":probe",
)
