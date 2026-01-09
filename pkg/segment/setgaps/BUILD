load("//tools:defs.bzl", "go_library")
load("//tools/go_generics:defs.bzl", "go_template_instance")

package(
    default_applicable_licenses = ["//:license"],
    default_visibility = ["//:sandbox"],
    licenses = ["notice"],
)

go_library(
    name = "setgaps",
    srcs = [
        ":setgaps_gen",
    ],
    stateify = False,
    deps = [
        "//pkg/segment",
        "//pkg/segment/rangetypes",
        "@org_golang_x_exp//constraints:go_default_library",
    ],
)

go_template_instance(
    name = "setgaps_gen",
    out = "set.go",
    package = "setgaps",
    template = "//pkg/segment/set:generic_set",
    consts = {
        "trackGaps": "1",
    },
    substrs = {
        "package setgaps\n\nimport": "// Package setgaps provides tools for working with collections of segments.\npackage setgaps\n\nimport",
    },
)
