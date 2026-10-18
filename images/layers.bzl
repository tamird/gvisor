"""Declared downloads and compressed layers for native OCI images."""

load("//tools:defs.bzl", "pkg_tar")
load("//tools/bazeldefs:platforms.bzl", "RBE_NETWORK_TOOLS_IMAGE")

_COREUTILS_TOOLCHAIN = "@bazel_lib//lib:coreutils_toolchain_type"
_COMPRESSION_CPUS = 4

def _downloaded_file_impl(ctx):
    coreutils = ctx.toolchains[_COREUTILS_TOOLCHAIN]
    output = ctx.actions.declare_file(ctx.label.name)
    if bool(ctx.attr.sha256) == bool(ctx.attr.md5):
        fail("Specify exactly one content checksum")

    # Fetch direct immutable URLs without requiring a registry /v2/ endpoint.
    # The pinned network image supplies curl; verify the complete response
    # before publishing it.
    # https://github.com/istio/istio/blob/1d6649895/docker/Dockerfile.base#L11-L15
    ctx.actions.run_shell(
        outputs = [output],
        tools = [coreutils.default.files],
        arguments = [ctx.attr.url, ctx.attr.sha256 or ctx.attr.md5, ctx.attr.size_bytes, output.path, coreutils.coreutils_info.bin.path, "sha256sum" if ctx.attr.sha256 else "md5sum"],
        command = """set -euo pipefail
trap 'result=$?; if (( result != 0 )); then "$5" rm -f -- "$4" || :; fi; exit "$result"' EXIT
# Include partial transfers (curl 18) in the bounded retries. Each retry starts
# from zero; retry-max-time limits retry starts, not the entire transfer.
/usr/bin/curl --fail --silent --show-error --location --retry 2 --retry-all-errors --retry-max-time 1800 --output "$4" "$1"
printf '%s  %s\\n' "$2" "$4" | "$5" "$6" --check
actual_size=$("$5" stat --format=%s "$4")
if [[ $actual_size != "$3" ]]; then
  printf 'Blob size mismatch: expected %s bytes, got %s\\n' "$3" "$actual_size" >&2
  exit 1
fi
""",
        mnemonic = "ImageDownload",
        progress_message = "Fetching immutable image input %s" % ctx.label.name,
        toolchain = _COREUTILS_TOOLCHAIN,
    )
    return [DefaultInfo(files = depset([output]))]

_downloaded_file = rule(
    implementation = _downloaded_file_impl,
    attrs = {
        "sha256": attr.string(),
        # Temporary capture input; replace GCS MD5 checks with measured SHA256.
        "md5": attr.string(),
        # attr.int is limited to signed 32-bit values; model blobs exceed 2 GiB.
        "size_bytes": attr.string(mandatory = True),
        "url": attr.string(mandatory = True),
    },
    toolchains = [_COREUTILS_TOOLCHAIN],
)

def _execution_properties(disk_bytes, network = False, cpus = None):
    properties = {
        "EstimatedFreeDiskBytes": str(disk_bytes + 4000000000),
        "EstimatedMemory": "2GB",
        "workload-isolation-type": "oci",
    }
    if cpus != None:
        properties["EstimatedComputeUnits"] = str(cpus)
    if network:
        properties.update({
            "container-image": RBE_NETWORK_TOOLS_IMAGE,
            "network": "external",
        })
    return select({
        Label("//tools/bazeldefs:rbe"): properties,
        "//conditions:default": {},
    })

def downloaded_file(name, url, size_bytes, sha256 = "", md5 = ""):
    """Downloads one immutable image input on the execution platform.

    Args:
      name: Output file target.
      url: Immutable HTTPS download URL.
      size_bytes: Expected uncompressed response size.
      sha256: Expected SHA256 digest.
      md5: Temporary transfer check while capturing missing SHA256 pins.
    """
    _downloaded_file(
        name = name,
        url = url,
        sha256 = sha256,
        md5 = md5,
        size_bytes = str(size_bytes),
        exec_compatible_with = ["@platforms//os:linux"],
        exec_properties = _execution_properties(size_bytes, network = True),
        tags = ["manual"],
    )

def compressed_layer(name, srcs, raw_size, **kwargs):
    """Packages image files directly into a deterministic parallel gzip stream.

    Args:
      name: Layer archive target.
      srcs: Declared files and packaging mappings.
      raw_size: Total input bytes for executor disk estimation.
      **kwargs: Additional pkg_tar attributes.
    """
    pkg_tar(
        name = name,
        srcs = srcs,
        compressor = "@pigz//:pigz",
        compressor_args = "-c -n -6 -p %d" % _COMPRESSION_CPUS,
        extension = "tar.gz",
        exec_properties = _execution_properties(2 * raw_size, cpus = _COMPRESSION_CPUS),
        mode = "0755",
        tags = ["manual"],
        **kwargs
    )
