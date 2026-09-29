"""Offline dependencies for builds of the exported Go module."""

load("@go_host_compatible_sdk_label//:defs.bzl", "HOST_COMPATIBLE_SDK")

def _module_proxy_impl(ctx):
    root = ctx.path(ctx.attr._sdk_root).dirname
    original = {name: ctx.read(label) for name, label in {
        "go.mod": ctx.attr.go_mod,
        "go.sum": ctx.attr.go_sum,
    }.items()}
    for name, content in original.items():
        ctx.file("module/" + name, content)
    result = ctx.execute(
        [str(root.get_child("bin/go")), "mod", "download"],
        working_directory = str(ctx.path("module")),
        timeout = 600,
        environment = {
            "GOENV": "off",
            "GOCACHE": str(ctx.path("build-cache")),
            "GOMAXPROCS": "4",
            "GOFLAGS": "",
            "GOMODCACHE": str(ctx.path("modules")),
            "GONOPROXY": "",
            "GONOSUMDB": "",
            "GOPATH": str(ctx.path("gopath")),
            "GOPRIVATE": "",
            "GOPROXY": "https://proxy.golang.org",
            "GOROOT": str(root),
            "GOSUMDB": "sum.golang.org",
            "GOTELEMETRY": "off",
            "GOTOOLCHAIN": "local",
            "GOVCS": "*:off",
            "GOWORK": "off",
        },
    )
    if result.return_code:
        fail("Downloading exported-module dependencies failed:\n" + result.stderr)
    for name, content in original.items():
        if ctx.read("module/" + name) != content:
            fail("go mod download changed %s; update the checked-in module metadata first" % name)

    # Go documents cache/download as a file:// module proxy. Only the proxy
    # protocol files are action inputs; no Gazelle cache or upgraded Bzlmod
    # dependency graph participates in this consumer-facing module build.
    # https://go.dev/ref/mod#module-cache
    ctx.file("modules/cache/download/ROOT", "")
    ctx.file("BUILD.bazel", """
package(default_visibility = ["//visibility:public"])
exports_files(["modules/cache/download/ROOT"])
filegroup(
    name = "files",
    srcs = glob([
        "modules/cache/download/**/*.info",
        "modules/cache/download/**/*.mod",
        "modules/cache/download/**/*.zip",
        "modules/cache/download/**/list",
    ]),
)
""")

module_proxy = repository_rule(
    implementation = _module_proxy_impl,
    attrs = {
        "go_mod": attr.label(mandatory = True, allow_single_file = True),
        "go_sum": attr.label(mandatory = True, allow_single_file = True),
        "_sdk_root": attr.label(default = HOST_COMPATIBLE_SDK),
    },
)
