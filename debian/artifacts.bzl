"""Stage the release packages in the architecture directories used by the publisher."""

load("//tools:arch.bzl", "arch_transition")

_ReleasePackages = provider(doc = "Original configured package files before staging links.", fields = ["files"])

_RELEASE_DIR = {"amd64": "x86_64", "arm64": "aarch64"}

def _release_artifacts_impl(ctx):
    outputs = []
    publisher_inputs = []
    for arch, targets in ctx.split_attr.srcs.items():
        for target in targets:
            for src in target[DefaultInfo].files.to_list():
                publisher_inputs.append(src)
                dest = ctx.actions.declare_file("%s/%s/%s" % (ctx.label.name, _RELEASE_DIR[arch], src.basename))
                ctx.actions.symlink(output = dest, target_file = src)
                outputs.append(dest)
    return [
        DefaultInfo(files = depset(outputs)),
        _ReleasePackages(files = depset(publisher_inputs)),
    ]

release_artifacts = rule(
    implementation = _release_artifacts_impl,
    attrs = {
        "srcs": attr.label_list(cfg = arch_transition, mandatory = True),
    },
)

def _release_inputs_impl(ctx):
    dist = ctx.file.python_dist
    if not dist.is_directory:
        fail("expected the Python distribution directory")
    inputs = depset([dist], transitive = [ctx.attr.packages[_ReleasePackages].files])
    manifest = ctx.actions.declare_file(ctx.label.name + ".inputs0")
    ctx.actions.write(manifest, "\000".join([f.path for f in inputs.to_list()]) + "\000")
    return [DefaultInfo(files = depset([manifest], transitive = [inputs]))]

release_inputs = rule(
    implementation = _release_inputs_impl,
    attrs = {
        "packages": attr.label(mandatory = True, providers = [_ReleasePackages]),
        "python_dist": attr.label(allow_single_file = True, mandatory = True),
    },
    doc = "Expose the configured packages and Python distribution for top-level download and publication.",
)
