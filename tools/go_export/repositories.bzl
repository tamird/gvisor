"""Offline dependencies resolved by the declared Go SDK."""

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
    selectors = [ctx.attr.goos, ctx.attr.goarch, ctx.attr.cgo_enabled]
    if ctx.attr.packages:
        if not all(selectors):
            fail("Package selection requires explicit goos, goarch and cgo_enabled")
        for package in ctx.attr.packages:
            # Go's wildcard traversal does not follow the staged symlink dirs.
            if not package.startswith("./") or ".." in package.split("/") or "..." in package:
                fail("Package roots must be exact paths within go_mod's source tree: " + package)
    elif any(selectors):
        fail("Go target selectors require packages")

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

    modules = ["all"]
    if ctx.attr.packages:
        # Keep writable manifests private while tracking every source change,
        # including newly added or removed files. Listing does not generate code.
        source = ctx.path(ctx.attr.go_mod).dirname
        ctx.watch_tree(source)
        for path in source.readdir():
            if path.basename not in original:
                ctx.symlink(path, "module/" + path.basename)
        environment.update({
            "GOOS": ctx.attr.goos,
            "GOARCH": ctx.attr.goarch,
            "CGO_ENABLED": ctx.attr.cgo_enabled,
        })

        # Let Go load the named packages and their dependencies instead of
        # prefetching every module requirement. Retain its module identities,
        # including replacements, below.
        # https://pkg.go.dev/cmd/go#hdr-List_packages_or_modules
        paths = _run_go(ctx, root, environment, [
            "list",
            "-deps",
            "-mod=readonly",
            "-f",
            "{{with .Module}}{{.Path}}{{end}}",
        ] + ctx.attr.packages)
        modules = sorted({path: None for path in paths.splitlines() if path})
        if not modules:
            fail("Package selection contains no Go modules")
    else:
        # download without arguments only covers go.mod's requirements on
        # current Go versions. list all reports the selected graph, including
        # replacements, rather than just the download subset.
        _run_go(ctx, root, environment, ["mod", "download"])
    inventory = _run_go(ctx, root, environment, ["list", "-m", "-json=Path,Version,Main,Replace,Error"] + modules)
    for name, content in original.items():
        if ctx.read("module/" + name) != content:
            fail("Go module resolution changed %s; update the checked-in module metadata first" % name)
    ctx.file("module-inventory.json", inventory)

    # Go documents cache/download as a file:// module proxy. Only its protocol
    # files are inputs to the consuming build; no Gazelle cache or
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
        "packages": attr.string_list(doc = "Optional exact package roots in go_mod's source tree; empty resolves the full module graph."),
        "goos": attr.string(doc = "GOOS for package selection."),
        "goarch": attr.string(doc = "GOARCH for package selection."),
        "cgo_enabled": attr.string(values = ["", "0", "1"], doc = "CGO_ENABLED for package selection."),
        "_sdk_root": attr.label(default = HOST_COMPATIBLE_SDK),
    },
)
