load("//tools:defs.bzl", "go_binary")
load(":defs.bzl", "host_tool")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

host_tool(name = "host_tool")

go_binary(
    name = "bzlformat",
    srcs = ["main.go"],
    importpath = "gvisor.dev/gvisor/tools/bzlformat",
    pure = True,
    visibility = ["//:__pkg__"],
)

go_binary(
    name = "fix",
    srcs = ["main.go"],
    args = [
        "fix",
        "$(rootpath :host_tool)",
        "$(rootpath @starlark_sources//:paths.json)",
    ],
    data = [
        ":host_tool",
        "@starlark_sources//:paths.json",
    ],
    importpath = "gvisor.dev/gvisor/tools/bzlformat",
    pure = True,
    visibility = ["//:__pkg__"],
)
