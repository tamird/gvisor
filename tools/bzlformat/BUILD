load("//tools:defs.bzl", "go_binary")
load(":defs.bzl", "native_tool")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

native_tool(name = "tool")

go_binary(
    name = "bzlformat",
    srcs = ["main.go"],
    args = [
        "fix",
        "$(rootpath :tool)",
        "$(rootpath @starlark_sources//:paths.json)",
    ],
    data = [
        ":tool",
        "@starlark_sources//:paths.json",
    ],
    importpath = "gvisor.dev/gvisor/tools/bzlformat",
    pure = True,
    visibility = ["//:__pkg__"],
)
