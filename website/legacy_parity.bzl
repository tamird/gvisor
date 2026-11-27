"""Retained SDK extraction, qualification only; not in the native proposal."""

load("@rules_img//img:providers.bzl", "ImageManifestInfo")

_TAR = "@tar.bzl//tar/toolchain:type"

def _sdk_impl(ctx):
    output = ctx.actions.declare_directory(ctx.label.name)

    # image_flatten supplies a single tar after applying all OCI whiteouts.
    blob = ctx.attr.image[ImageManifestInfo].layers[0].blob
    tar = ctx.toolchains[_TAR]
    args = ctx.actions.args()
    args.add_all(["-xf", blob.path, "-C", output.path, "--no-same-owner", "--no-xattrs", "--no-acls", "--no-fflags"])
    for pattern in ["usr/local/bin/ruby", "usr/bin/node", "lib/*.so*", "usr/lib/*.so*", "usr/local/lib/*.so*", "usr/local/lib/ruby", "usr/gem", "usr/share/zoneinfo/America/Chicago"]:
        args.add("--include", pattern)
    for pattern in ["usr/gem/cache", "usr/gem/doc", "usr/local/lib/ruby/gems/2.7.0/cache", "usr/local/lib/ruby/gems/2.7.0/doc", ".git"]:
        args.add("--exclude", pattern)
    ctx.actions.run(
        executable = tar.tarinfo.binary,
        inputs = depset([blob], transitive = [tar.default.files]),
        outputs = [output],
        arguments = [args],
        mnemonic = "JekyllSDK",
        toolchain = _TAR,
    )
    return [DefaultInfo(files = depset([output]))]

jekyll_sdk = rule(
    implementation = _sdk_impl,
    attrs = {"image": attr.label(mandatory = True, providers = [ImageManifestInfo])},
    toolchains = [_TAR],
)
