load("//tools:defs.bzl", "go_library", "go_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_library(
    name = "wire",
    srcs = [
        "capture.go",
        "wire.go",
    ],
    marshal = False,
    stateify = False,
    visibility = ["//:sandbox"],
    deps = [
        "//pkg/gohacks",
    ],
)

go_test(
    name = "wire_test",
    srcs = ["wire_test.go"],
    library = ":wire",
)
