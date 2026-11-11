load("//tools:defs.bzl", "go_binary")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_binary(
    name = "runsc-netgofer",
    srcs = [
        "main.go",
    ],
    noasan = True,
    pure = True,
    visibility = [
        "//:sandbox",
    ],
    deps = [
        "//runsc/cli",
        "//runsc/cmd/util",
        "//runsc/gofer/net",
        "//runsc/gvisorbinaries",
    ],
)
