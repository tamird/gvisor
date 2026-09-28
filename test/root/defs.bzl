"""Declared inputs for the existing containerd/CRI test matrix."""

load("@rules_oci//oci:defs.bzl", "oci_load")
load("//test/docker:config.bzl", "CONTAINERD_IMAGES")
load("//test/docker:defs.bzl", "docker_test")
load("//tools:arch.bzl", "select_arch")

# Match containerd-tests in Make and each release's default sandbox image.
# https://github.com/containerd/containerd/blob/v1.7.31/pkg/cri/config/config_unix.go#L96
# https://github.com/containerd/containerd/blob/v2.0.8/internal/cri/config/config.go#L73
# https://github.com/containerd/containerd/blob/v2.1.7/internal/cri/config/config.go#L76
# https://github.com/containerd/containerd/blob/v2.2.3/internal/cri/config/config.go#L76
_CONTAINERD_VERSIONS = {
    "1.7.31": "3.8",
    "2.0.8": "3.10",
    "2.1.7": "3.10",
    "2.2.3": "3.10.1",
}

def _image_config_impl(ctx):
    images = {}
    archives = []
    for target, image in ctx.attr.images.items():
        files = target[DefaultInfo].files.to_list()
        if len(files) != 1:
            fail("%s must supply exactly one image archive" % target.label)
        if image in images:
            fail("duplicate image name: %s" % image)
        images[image] = files[0].short_path
        archives.append(files[0])
    output = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.write(output, json.encode({
        "containerd_version": ctx.attr.containerd_version,
        "sandbox_image": ctx.attr.sandbox_image,
        "images": images,
    }))
    return [DefaultInfo(files = depset([output]), runfiles = ctx.runfiles(files = archives))]

_image_config = rule(
    implementation = _image_config_impl,
    attrs = {
        "containerd_version": attr.string(mandatory = True),
        "sandbox_image": attr.string(mandatory = True),
        "images": attr.label_keyed_string_dict(allow_files = True, mandatory = True),
    },
)

def containerd_test(name, **kwargs):
    """Keeps the installed entrypoint and declares each existing version's inputs.

    Args:
      name: Existing test target name.
      **kwargs: Remaining docker_test arguments, shared by all entrypoints.
    """
    pauses = {version: True for version in _CONTAINERD_VERSIONS.values()}
    for version in sorted(pauses):
        for arch in ["amd64", "arm64"]:
            target = name + "_pause_" + version.replace(".", "_") + "_" + arch
            oci_load(
                name = target,
                image = "@containerd_pause_" + version.replace(".", "_") + "_" + arch,
                repo_tags = ["registry.k8s.io/pause:" + version],
                tags = ["manual"],
            )
            native.filegroup(
                name = target + "_tar",
                srcs = [":" + target],
                output_group = "tarball",
                tags = ["manual"],
            )

    variants = []
    for version, pause in _CONTAINERD_VERSIONS.items():
        suffix = version.replace(".", "_")
        config = name + "_" + suffix + "_image_config"
        image_inputs = {}
        for arch in ["amd64", "arm64"]:
            images = {
                "//test/docker:images_" + image.replace("/", "_").replace("-", "_") + "_" + arch + "_tar": image
                for image in CONTAINERD_IMAGES
            }
            images[":" + name + "_pause_" + pause.replace(".", "_") + "_" + arch + "_tar"] = "registry.k8s.io/pause:" + pause
            image_inputs[arch] = images
        _image_config(
            name = config,
            testonly = True,
            containerd_version = version,
            sandbox_image = "registry.k8s.io/pause:" + pause,
            images = select_arch(**image_inputs),
            tags = ["manual"],
        )
        variants.append(struct(
            name = suffix,
            args = [],
            test_args = [
                "--containerd_version=" + version,
                "--harness_image_config=$(rootpath :" + config + ")",
            ],
            data = [":" + config],
        ))
    docker_test(
        name = name,
        cohort = "containerd",
        runtime_variants = variants,
        **kwargs
    )
