load("//tools:defs.bzl", "go_binary")
load(":diagnostic.bzl", "profile_decoders")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_binary(
    name = "profiletool",
    srcs = ["profiletool.go"],
    visibility = [
        "//:sandbox",
    ],
    deps = [
        "//pkg/log",
        "//runsc/flag",
        "@com_github_google_pprof//profile:go_default_library",
    ],
)

# Fork-only tools for reading already-collected PHP diagnostic artifacts.
profile_decoders(
    name = "diagnostic_decoders",
    tags = ["manual"],
)
