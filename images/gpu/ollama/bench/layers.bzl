"""Package Ollama's immutable registry blobs with the standard OCI tools."""

load("@rules_oci//oci:defs.bzl", "oci_image")
load("@rules_pkg//pkg:mappings.bzl", "pkg_attributes", "pkg_files")
load("//tools:defs.bzl", "pkg_tar")
load("//tools/bazeldefs:platforms.bzl", "RBE_NETWORK_TOOLS_IMAGE")

_CRANE_TOOLCHAIN = "@rules_oci//oci:crane_toolchain_type"

def _blob_impl(ctx):
    crane = ctx.toolchains[_CRANE_TOOLCHAIN].crane_info.binary
    output = ctx.actions.declare_file(ctx.label.name)

    # crane blob drains a SHA256-verifying reader. Its output is registry bytes,
    # which are raw model data here, not a gzip-compressed container layer.
    # https://github.com/google/go-containerregistry/blob/a0658aa1d/internal/verify/verify.go#L54-L99
    ctx.actions.run_shell(
        outputs = [output],
        tools = [crane],
        arguments = [crane.path, ctx.attr.reference, output.path],
        command = 'exec "$1" blob "$2" > "$3"',
        mnemonic = "OllamaBlob",
        progress_message = "Fetching immutable Ollama blob %s" % ctx.label.name,
        toolchain = _CRANE_TOOLCHAIN,
    )
    return [DefaultInfo(files = depset([output]))]

_blob = rule(
    implementation = _blob_impl,
    attrs = {"reference": attr.string(mandatory = True)},
    toolchains = [_CRANE_TOOLCHAIN],
)

def _execution_properties(disk_bytes, network = False):
    properties = {
        "EstimatedFreeDiskBytes": str(disk_bytes + 4000000000),
        "EstimatedMemory": "2GB",
        "workload-isolation-type": "oci",
    }
    if network:
        properties.update({
            "container-image": RBE_NETWORK_TOOLS_IMAGE,
            "network": "external",
        })
    return select({
        Label("//tools/bazeldefs:rbe"): properties,
        "//conditions:default": {},
    })

def model_layer_name(model):
    """Returns the layer target name for a library model name:tag."""
    return model.replace(":", "_") + "_layer"

def model_layers(name, models):
    """Declares each blob once and a deterministic gzip tar for each model.

    Args:
      name: Prefix for the immutable blob targets.
      models: Model metadata generated from the checked-in raw manifests.
    """
    declared = {}
    for model, metadata in models.items():
        files = [metadata.manifest]
        model_name, _, tag = model.partition(":")
        renames = {metadata.manifest: "root/.ollama/models/manifests/registry.ollama.ai/library/%s/%s" % (model_name, tag)}
        raw_size = 0
        for blob in metadata.blobs:
            digest = blob["digest"]
            target = name + "_blob_" + digest[7:]
            if digest not in declared:
                _blob(
                    name = target,
                    reference = metadata.repository + "@" + digest,
                    exec_compatible_with = ["@platforms//os:linux"],
                    exec_properties = _execution_properties(blob["size"], network = True),
                    tags = ["manual"],
                )
                declared[digest] = blob["size"]
            elif declared[digest] != blob["size"]:
                fail("Conflicting sizes for blob %s" % digest)
            files.append(":" + target)
            renames[":" + target] = "root/.ollama/models/blobs/" + digest.replace(":", "-")
            raw_size += blob["size"]
        layer = model_layer_name(model)
        pkg_files(
            name = layer + "_files",
            srcs = files,
            renames = renames,
            attributes = pkg_attributes(mode = "0644"),
            tags = ["manual"],
        )
        pkg_tar(
            name = layer,
            srcs = [":" + layer + "_files"],
            # rules_pkg writes gzip directly, without staging an uncompressed
            # tar beside the raw blobs and compressed output.
            extension = "tar.gz",
            exec_properties = _execution_properties(2 * raw_size),
            mode = "0755",
            tags = ["manual"],
            visibility = ["//images/gpu/ollama/bench:__subpackages__"],
        )

def model_image(name, layers, exec_properties):
    """Uses the pinned server and the benchmark's existing runtime environment.

    Args:
      name: Image target name.
      layers: Declared model layer archives in their image order.
      exec_properties: Resources for the image's declared inputs.
    """
    oci_image(
        name = name,
        base = "@ollama_server_linux_amd64",
        exec_properties = exec_properties,
        env = {
            "OLLAMA_HOST": "0.0.0.0:11434",
            "OLLAMA_ORIGINS": "*",
            "PATH": "$PATH:/usr/local/nvidia/bin",
        },
        tags = ["manual"],
        tars = layers,
    )
