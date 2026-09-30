"""Offline dependencies for builds of the exported Go module."""

load("@go_host_compatible_sdk_label//:defs.bzl", "HOST_COMPATIBLE_SDK")

def _run_go(ctx, root, environment, args):
    result = ctx.execute(
        [str(root.get_child("bin/go"))] + args,
        working_directory = str(ctx.path("module")),
        timeout = 600,
        environment = environment,
    )
    if result.return_code:
        fail("go %s failed:\n%s" % (" ".join(args), result.stderr))
    return result.stdout

def _module_proxy_impl(ctx):
    root = ctx.path(ctx.attr._sdk_root).dirname
    original = {name: ctx.read(label) for name, label in {
        "go.mod": ctx.attr.go_mod,
        "go.sum": ctx.attr.go_sum,
    }.items()}
    for name, content in original.items():
        ctx.file("module/" + name, content)
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
    }

    # download without arguments only covers go.mod's requirements on current
    # Go versions. list all reports the selected graph, including replacements,
    # so the license audit does not mistake the download subset for that graph.
    _run_go(ctx, root, environment, ["mod", "download"])
    inventory = _run_go(ctx, root, environment, ["list", "-m", "-json=Path,Version,Main,Replace,Error", "all"])
    for name, content in original.items():
        if ctx.read("module/" + name) != content:
            fail("Go module resolution changed %s; update the checked-in module metadata first" % name)
    ctx.file("module-inventory.json", inventory)

    # Go documents cache/download as a file:// module proxy. Only its protocol
    # files are inputs to the exported-module build; no Gazelle cache or
    # upgraded Bzlmod graph participates in that build.
    # https://go.dev/ref/mod#module-cache
    ctx.file("modules/cache/download/ROOT", "")
    ctx.file("BUILD.bazel", """
load("@bazel_skylib//rules:copy_file.bzl", "copy_file")

package(default_visibility = ["//visibility:public"])
exports_files(["modules/cache/download/ROOT"])
copy_file(
    name = "license_inventory",
    src = "module-inventory.json",
    out = "license-inventory.json",
)
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
