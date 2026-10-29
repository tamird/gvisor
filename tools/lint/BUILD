load("@bazel_skylib//rules:native_binary.bzl", "native_binary")
load(":cpplint.bzl", "CPPLINT_FLAGS")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

native_binary(
    name = "cpplint",
    src = "@cpplint",
    args = CPPLINT_FLAGS,
)
