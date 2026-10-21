"""Archive interfaces for images assembled with rules_img."""

load("@rules_img//img:load.bzl", "image_load")
load("//test/docker:source_image_contracts.bzl", "declared_image_archive")
load("//tools:arch.bzl", "select_arch")
load(":archive.bzl", "image_context")

def image_outputs(name, image):
    """Exposes an image manifest to the existing tests and Make targets.

    Args:
      name: The archive target name.
      image: The package's image_manifest target.
    """
    package = native.package_name()
    image_load(
        name = "load_image",
        image = image,
        tag = "gvisor.dev/%s:latest" % package,
        tags = ["manual"],
    )
    native.filegroup(
        name = name + "_tarball",
        srcs = [":load_image"],
        output_group = "tarball",
        tags = ["manual"],
    )
    declared_image_archive(
        name = name,
        archive = ":" + name + "_tarball",
        architecture = select_arch(amd64 = "amd64", arm64 = "arm64"),
        image = package.removeprefix("images/"),
        origin = "native-img",
        manifest = image,
        tags = ["manual"],
        visibility = ["//test/docker:__pkg__"],
    )
    image_context(archive = ":" + name)
