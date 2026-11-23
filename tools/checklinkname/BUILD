load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "checklinkname",
    srcs = ["check_linkname.go"],
    importpath = "gvisor.dev/gvisor/tools/checklinkname",
    nogo = False,
    stateify = False,
    visibility = ["//tools/nogo:__subpackages__"],
    deps = ["@org_golang_x_tools//go/analysis:go_default_library"],
)

go_test(
    name = "checklinkname_test",
    size = "small",
    srcs = ["check_linkname_test.go"],
    embed = [":checklinkname"],
)
