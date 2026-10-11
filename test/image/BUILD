load("//test/docker:config.bzl", "INTEGRATION_VARIANTS", "PLUGIN_NETWORK_VARIANT")
load("//test/docker:defs.bzl", "docker_test")
load("//tools:defs.bzl", "go_library")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

docker_test(
    name = "image_test",
    size = "large",
    srcs = [
        "image_test.go",
    ],
    cohort = "image",
    data = [
        "latin10k.txt",
        "mysql.sql",
        # Needed to invalidate the bazel cache in case of any code changes.
        "//:release",
    ],
    embed = [":image"],
    runtime_variants = INTEGRATION_VARIANTS + [PLUGIN_NETWORK_VARIANT],
    tags = ["manual"],
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/test/dockerutil",
        "//pkg/test/testutil",
        "@com_github_cenkalti_backoff//:go_default_library",
        "@in_gopkg_yaml_v3//:go_default_library",
    ],
)

go_library(
    name = "image",
    srcs = ["image.go"],
)
