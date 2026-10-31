load("//tools:defs.bzl", "go_binary", "go_library", "go_test", "version_x_defs")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_test(
    name = "parsers_test",
    size = "small",
    srcs = ["go_parser_test.go"],
    embed = [":parsers"],
    nogo = False,
    deps = [
        "//tools/bigquery",
        "@com_github_google_go_cmp//cmp:go_default_library",
    ],
)

go_library(
    name = "parsers",
    testonly = 1,
    srcs = [
        "go_parser.go",
    ],
    nogo = False,
    visibility = ["//:sandbox"],
    deps = [
        "//test/benchmarks/tools",
        "//tools/bigquery",
    ],
)

go_binary(
    name = "parser",
    testonly = 1,
    srcs = [
        "parser_main.go",
        "version.go",
    ],
    nogo = False,
    x_defs = version_x_defs("main.version"),
    deps = [
        ":parsers",
        "//runsc/flag",
        "//tools/bigquery",
    ],
)
