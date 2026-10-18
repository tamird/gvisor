"""Package Ollama's immutable registry blobs with the standard OCI tools."""

load("@rules_oci//oci:defs.bzl", "oci_image")
load("@rules_pkg//pkg:mappings.bzl", "pkg_attributes", "pkg_files")
load("//tools:defs.bzl", "pkg_tar")
load("//tools/bazeldefs:platforms.bzl", "RBE_NETWORK_TOOLS_IMAGE")

_COREUTILS_TOOLCHAIN = "@bazel_lib//lib:coreutils_toolchain_type"

def _blob_impl(ctx):
    coreutils = ctx.toolchains[_COREUTILS_TOOLCHAIN]
    output = ctx.actions.declare_file(ctx.label.name)

    # Ollama serves immutable blob URLs but returns 404 for the registry /v2/
    # probe required by crane. The pinned network image supplies curl; verify
    # its raw response against the checked-in manifest before publishing it.
    # https://github.com/istio/istio/blob/1d6649895/docker/Dockerfile.base#L11-L15
    ctx.actions.run_shell(
        outputs = [output],
        tools = [coreutils.default.files],
        arguments = [ctx.attr.url, ctx.attr.sha256, ctx.attr.size_bytes, output.path, coreutils.coreutils_info.bin.path],
        command = """set -euo pipefail
trap 'result=$?; if (( result != 0 )); then "$5" rm -f -- "$4" || :; fi; exit "$result"' EXIT
# Include partial transfers (curl 18) in the bounded retries. Each retry starts
# from zero; retry-max-time limits retry starts, not the entire transfer.
/usr/bin/curl --fail --silent --show-error --location --retry 2 --retry-all-errors --retry-max-time 1800 --output "$4" "$1"
printf '%s  %s\\n' "$2" "$4" | "$5" sha256sum --check
actual_size=$("$5" stat --format=%s "$4")
if [[ $actual_size != "$3" ]]; then
  printf 'Blob size mismatch: expected %s bytes, got %s\\n' "$3" "$actual_size" >&2
  exit 1
fi
""",
        mnemonic = "OllamaBlob",
        progress_message = "Fetching immutable Ollama blob %s" % ctx.label.name,
        toolchain = _COREUTILS_TOOLCHAIN,
    )
    return [DefaultInfo(files = depset([output]))]

_blob = rule(
    implementation = _blob_impl,
    attrs = {
        "sha256": attr.string(mandatory = True),
        # attr.int is limited to signed 32-bit values; model blobs exceed 2 GiB.
        "size_bytes": attr.string(mandatory = True),
        "url": attr.string(mandatory = True),
    },
    toolchains = [_COREUTILS_TOOLCHAIN],
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
                    url = "https://" + metadata.repository.replace("/library/", "/v2/library/", 1) + "/blobs/" + digest,
                    sha256 = digest[7:],
                    size_bytes = str(blob["size"]),
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
