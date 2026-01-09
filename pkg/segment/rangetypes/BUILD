load("//tools:defs.bzl", "go_library")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//:sandbox"],
    licenses = ["notice"],
)

go_library(
    name = "rangetypes",
    srcs = ["range.go"],
    stateify = False,
    deps = ["@org_golang_x_exp//constraints:go_default_library"],
)
