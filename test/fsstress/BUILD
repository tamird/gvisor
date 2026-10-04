load("//test/docker:defs.bzl", "docker_test")
load("//tools:defs.bzl", "go_library")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

docker_test(
    name = "fsstress_test",
    size = "large",
    srcs = [
        "fsstress_test.go",
    ],
    cohort = "fsstress",
    library = ":fsstress",
    tags = ["manual"],
    deps = [
        "//pkg/test/dockerutil",
        "//pkg/test/testutil",
        "@com_github_docker_docker//api/types/mount:go_default_library",
    ],
)

go_library(
    name = "fsstress",
    srcs = ["fsstress.go"],
)
