load("@bazel_skylib//rules:build_test.bzl", _stock = "build_test")
load("//tools:defs.bzl", "go_binary")
load(":controls.bzl", "barrier_test", "defaults_test", "dependency")

package(default_visibility = ["//visibility:public"])

go_binary(name = "probe", srcs = ["probe.go"], pure = True, static = True, testonly = True)

dependency(name = "valid", testonly = True)

[
    dependency(
        name = form + "_" + state + route + "_input",
        fail = state == "broken",
        testonly = True,
    )
    for form in ["defaults", "barrier"]
    for state in ["valid", "broken"]
    for route in ["", "_via_suite"]
]

[
    rule(
        name = form + "_" + state + route,
        targets = [":" + form + "_" + state + route + "_input"],
    )
    for form, rule in [("defaults", defaults_test), ("barrier", barrier_test)]
    for state in ["valid", "broken"]
    for route in ["", "_via_suite"]
]

[
    test_suite(
        name = form + "_" + state + "_suite",
        tests = [":" + form + "_" + state + "_via_suite"],
    )
    for form in ["defaults", "barrier"]
    for state in ["valid", "broken"]
]

_stock(name = "stock_small", targets = [":valid"])
