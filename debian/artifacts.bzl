"""Stage the release packages in the architecture directories used by the publisher."""

load("//tools:arch.bzl", "arch_transition")

_RELEASE_DIR = {"amd64": "x86_64", "arm64": "aarch64"}

def _release_artifacts_impl(ctx):
    outputs = []
    for arch, targets in ctx.split_attr.srcs.items():
        for target in targets:
            for src in target[DefaultInfo].files.to_list():
                dest = ctx.actions.declare_file("%s/%s/%s" % (ctx.label.name, _RELEASE_DIR[arch], src.basename))
                ctx.actions.symlink(output = dest, target_file = src)
                outputs.append(dest)
    return [DefaultInfo(files = depset(outputs))]

release_artifacts = rule(
    implementation = _release_artifacts_impl,
    attrs = {
        "srcs": attr.label_list(cfg = arch_transition, mandatory = True),
    },
)
