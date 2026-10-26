"""Expose pinned Ollama manifests to BUILD without downloading model data."""

def _models_impl(ctx):
    models = []
    manifests = []
    for source, model in ctx.attr.manifests.items():
        name, separator, tag = model.partition(":")
        if not separator or not name or not tag or "/" in model:
            fail("Expected a library model name:tag, got %r" % model)
        manifest = json.decode(ctx.read(source))
        if manifest.get("schemaVersion") != 2 or manifest.get("mediaType") != "application/vnd.docker.distribution.manifest.v2+json":
            fail("Unsupported Ollama manifest: %s" % source)
        blobs = [manifest["config"]] + manifest["layers"]
        for blob in blobs:
            digest = blob["digest"]
            if not digest.startswith("sha256:") or len(digest) != 71 or [c for c in digest[7:].elems() if c not in "0123456789abcdef"]:
                fail("Expected a SHA256 blob digest in %s, got %r" % (source, digest))
            if type(blob["size"]) != "int" or blob["size"] < 0:
                fail("Invalid blob size in %s" % source)
        filename = name + "_" + tag + ".json"
        ctx.symlink(ctx.path(source), filename)
        manifests.append(filename)
        models.append("%r: struct(manifest = Label(%r), repository = %r, blobs = %r)" % (
            model,
            "//:" + filename,
            "registry.ollama.ai/library/" + name,
            [{"digest": blob["digest"], "size": blob["size"]} for blob in blobs],
        ))
    ctx.file("models.bzl", "# Generated from the checked-in raw manifests.\nMODELS = {\n" + ",\n".join(models) + "\n}\n")
    ctx.file("BUILD.bazel", "exports_files(%r)\n" % manifests)

ollama_models = repository_rule(
    implementation = _models_impl,
    attrs = {
        "manifests": attr.label_keyed_string_dict(
            doc = "Raw manifest files mapped to their library model name:tag.",
            allow_files = [".json"],
            mandatory = True,
        ),
    },
)
