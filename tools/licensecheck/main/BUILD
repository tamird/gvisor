load("//tools:defs.bzl", "go_binary")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_binary(
    name = "licensecheck",
    srcs = ["main.go"],
    # make run executes the binary without these args and retains its Bazel wrapper.
    args = ["--bazel=bazel"],
    deps = ["//tools/licensecheck"],
)
