"""Package Ollama's immutable registry blobs with the standard OCI tools."""

load("@rules_oci//oci:defs.bzl", "oci_image")
load("@rules_pkg//pkg:mappings.bzl", "pkg_attributes", "pkg_files")
load("//images:layers.bzl", "compressed_layer", "downloaded_file")

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
                downloaded_file(
                    name = target,
                    url = "https://" + metadata.repository.replace("/library/", "/v2/library/", 1) + "/blobs/" + digest,
                    sha256 = digest[7:],
                    size_bytes = blob["size"],
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
        compressed_layer(
            name = layer,
            srcs = [":" + layer + "_files"],
            raw_size = raw_size,
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
