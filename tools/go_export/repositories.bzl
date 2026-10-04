"""Offline dependencies resolved by the declared Go SDK."""

load("@go_host_compatible_sdk_label//:defs.bzl", "HOST_COMPATIBLE_SDK")

def _run_go(ctx, root, environment, args, working_directory = "module"):
    result = ctx.execute(
        [str(root.get_child("bin/go"))] + args,
        working_directory = str(ctx.path(working_directory)),
        timeout = 600,
        environment = environment,
    )
    if result.return_code:
        fail("go %s failed:\n%s" % (" ".join(args), result.stderr))
    return result.stdout

def _add_resolved_modules(ctx, root, environment, original):
    """Seed root Go declarations using their resolved Gazelle archive pins."""
    metadata = json.decode(ctx.read(ctx.attr.resolved_modules))
    original_module = json.decode(_run_go(ctx, root, environment, ["mod", "edit", "-json"]))
    original_requirements = {module["Path"]: None for module in (original_module.get("Require") or [])}
    checksums = {}
    for line in original["go.sum"].splitlines():
        path, version, checksum = line.split(" ")
        checksums[(path, version)] = checksum
    root_requirements = {path: None for path in metadata["root_requirements"]}
    edits = []
    for name, module in sorted(metadata["archives"].items()):
        if module.get("local_path") or module.get("urls"):
            fail("Go analysis cannot represent local or archive override " + name)
        path = module["importpath"]
        if path not in root_requirements:
            continue
        version = module.get("version")
        checksum = module.get("sum")
        if not version or not checksum:
            fail("Go analysis requires a version and checksum for " + name)
        edits.append("-require=%s@%s" % (path, module["requirement_version"]))
        actual = module.get("replace") or path
        if module.get("replace"):
            edits.append("-replace=%s=%s@%s" % (path, actual, version))
        key = (actual, version)
        if key in checksums and checksums[key] != checksum:
            fail("Conflicting checksums for %s@%s" % key)
        checksums[key] = checksum
    _run_go(ctx, root, environment, ["mod", "edit"] + edits)
    ctx.file("module/go.sum", "".join([
        "%s %s %s\n" % (path, version, checksum)
        for (path, version), checksum in sorted(checksums.items())
    ]))

    # The root Go module already represents some Bazel-provided dependencies
    # as module archives. Other selected providers need an explicit Go input.
    return {
        path: module
        for path, module in metadata["bazel_modules"].items()
        if not module["is_root"] and path not in original_requirements
    }

def _module_proxy_impl(ctx):
    root = ctx.path(ctx.attr._sdk_root).dirname
    selectors = [ctx.attr.goos, ctx.attr.goarch, ctx.attr.cgo_enabled]
    if bool(ctx.attr.resolved_modules) != bool(ctx.attr.source_manifest):
        fail("Analysis requires both resolved_modules and source_manifest")
    if ctx.attr.packages or ctx.attr.source_manifest:
        if not all(selectors):
            fail("Source selection requires explicit goos, goarch and cgo_enabled")
        for package in ctx.attr.packages:
            # Go's wildcard traversal does not follow the staged symlink dirs.
            if not package.startswith("./") or ".." in package.split("/") or "..." in package:
                fail("Package roots must be exact paths within go_mod's source tree: " + package)
    elif any(selectors):
        fail("Go target selectors require packages or source_manifest")

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

    bazel_modules = {}
    if ctx.attr.resolved_modules:
        if ctx.attr.packages:
            fail("Resolved module metadata requires full graph selection")
        bazel_modules = _add_resolved_modules(ctx, root, environment, original)

        # Resolve seeded minimum versions before package loading can download
        # their sources. The final download below includes any requirements
        # added by loading the indexed packages.
        _run_go(ctx, root, environment, ["mod", "download", "all"])

        # Match the analysis action's indexed paths and current file contents.
        # Real parent directories let ./... traverse only those sources.
        # Watch each file so content edits invalidate resolution even when
        # the manifest's indexed path list remains unchanged.
        manifest = ctx.path(ctx.attr.source_manifest)
        for number, name in enumerate(json.decode(ctx.read(manifest))):
            source = manifest.dirname.get_child("files", str(number))
            ctx.watch(source)
            ctx.symlink(source, "source/" + name)

        # Module selection alone can prune older go.mod files that loading
        # actual packages needs. Use the extractor's native metadata command
        # before downloading the final selected graph. Package errors remain
        # in the output, including unavailable generated repository sources.
        # https://github.com/github/codeql/blob/6e9f9e383/go/extractor/toolchain/toolchain.go
        package_environment = dict(environment, **{
            "GOOS": ctx.attr.goos,
            "GOARCH": ctx.attr.goarch,
            "CGO_ENABLED": ctx.attr.cgo_enabled,
            "GOFLAGS": "-modfile=%s -mod=mod -buildvcs=false" % ctx.path("module/go.mod"),
        })
        ctx.file("package-inventory.json", _run_go(
            ctx,
            root,
            package_environment,
            ["list", "-e", "-f", "", "-deps", "-json", "./..."],
            working_directory = "source",
        ))

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
        # Populate the complete selected graph, including transitive modules
        # that package loading may need. Bare download only fetches modules
        # explicitly required by go.mod when it declares Go 1.17 or newer.
        # https://pkg.go.dev/cmd/go#hdr-Download_modules_to_local_cache
        _run_go(ctx, root, environment, ["mod", "download", "all"])
    if bazel_modules:
        selected = _run_go(ctx, root, environment, ["list", "-m", "-f", "{{.Path}}", "all"])
        for path in selected.splitlines():
            if path in bazel_modules:
                fail("Go analysis needs %s, which is supplied by Bazel module %s rather than a declared module archive" % (path, bazel_modules[path]["module_name"]))
    inventory = _run_go(ctx, root, environment, ["list", "-m", "-json=Path,Version,Main,Replace,Error"] + modules)

    # Go owns the derived analysis manifests: download all may raise seeded
    # minimum requirements and add authenticated transitive checksums. Publish
    # those native outputs and the selected inventory, while source profiles
    # continue to require their checked-in manifests to remain unchanged.
    if not ctx.attr.resolved_modules:
        for name, content in original.items():
            if ctx.read("module/" + name) != content:
                fail("Go module resolution changed %s; update the checked-in module metadata first" % name)
    ctx.file("module-inventory.json", inventory)

    # Go documents cache/download as a file:// module proxy. Only its protocol
    # files are inputs to the consuming build; its actions never fetch modules.
    # The exported profile uses checked-in manifests; analysis additionally
    # incorporates resolved archive pins for the root's Go declarations.
    # https://go.dev/ref/mod#module-cache
    ctx.file("modules/cache/download/ROOT", "")
    ctx.file("BUILD.bazel", """
load("@bazel_skylib//rules:copy_file.bzl", "copy_file")

package(default_visibility = ["//visibility:public"])
exports_files(["modules/cache/download/ROOT"] + %s)
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
""" % json.encode(["module/go.mod", "module/go.sum", "package-inventory.json"] if ctx.attr.resolved_modules else []))

module_proxy = repository_rule(
    implementation = _module_proxy_impl,
    attrs = {
        "go_mod": attr.label(mandatory = True, allow_single_file = True),
        "go_sum": attr.label(mandatory = True, allow_single_file = True),
        "resolved_modules": attr.label(allow_single_file = True, doc = "Optional Gazelle module archive metadata for a distinct analysis profile; does not apply Bazel dependency source patches."),
        "source_manifest": attr.label(allow_single_file = True, doc = "Indexed analysis paths with numbered files alongside the manifest; required with resolved_modules."),
        "packages": attr.string_list(doc = "Optional exact package roots in go_mod's source tree; empty resolves the full module graph."),
        "goos": attr.string(doc = "GOOS for package selection."),
        "goarch": attr.string(doc = "GOARCH for package selection."),
        "cgo_enabled": attr.string(values = ["", "0", "1"], doc = "CGO_ENABLED for package selection."),
        "_sdk_root": attr.label(default = HOST_COMPATIBLE_SDK),
    },
)
