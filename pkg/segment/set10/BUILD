load("//tools:defs.bzl", "go_library")
load("//tools/go_generics:defs.bzl", "go_template_instance")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//:sandbox"],
    licenses = ["notice"],
)

go_library(
    name = "set10",
    srcs = [
        ":set10_gen",
    ],
    stateify = False,
    deps = [
        "//pkg/segment",
        "//pkg/segment/rangetypes",
        "@org_golang_x_exp//constraints:go_default_library",
    ],
)

go_template_instance(
    name = "set10_gen",
    out = "set.go",
    package = "set10",
    template = "//pkg/segment/set:generic_set",
    consts = {
        "minDegree": "10",
    },
    substrs = {
        "package set10\n\nimport": "// Package set10 provides tools for working with collections of segments.\npackage set10\n\nimport",
    },
)
