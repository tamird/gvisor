load("//tools:defs.bzl", "go_library")
load("//tools/go_generics:defs.bzl", "go_template_instance")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//:sandbox"],
    licenses = ["notice"],
)

go_library(
    name = "set8gaps",
    srcs = [
        ":set8gaps_gen",
    ],
    stateify = False,
    deps = [
        "//pkg/segment",
        "//pkg/segment/rangetypes",
        "@org_golang_x_exp//constraints:go_default_library",
    ],
)

go_template_instance(
    name = "set8gaps_gen",
    out = "set.go",
    package = "set8gaps",
    template = "//pkg/segment/set:generic_set",
    consts = {
        "minDegree": "8",
        "trackGaps": "1",
    },
    substrs = {
        "package set8gaps\n\nimport": "// Package set8gaps provides tools for working with collections of segments.\npackage set8gaps\n\nimport",
    },
)
