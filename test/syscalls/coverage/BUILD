load("@bazel_skylib//rules:native_binary.bzl", "native_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

native_test(
    name = "uname_loader_test",
    src = "//test/syscalls/linux:uname_test",
    args = ["--gtest_filter=UnameTest.Sanity"],
    tags = ["manual"],
)
