load("//tools:defs.bzl", "go_binary")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_binary(
    name = "runner",
    testonly = 1,
    srcs = ["main.go"],
    data = ["//images:runtimes/nodejs22.2.0/tls-servername.patch"],
    visibility = ["//test/runtimes:__pkg__"],
    deps = [
        "//pkg/test/dockerutil",
        "//pkg/test/testutil",
        "//test/runtimes/runner/lib",
    ],
)
