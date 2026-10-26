"""Test image archives and contexts for the Make image targets."""

load("@rules_img//img:load.bzl", "image_load")
load("@rules_pkg//pkg:mappings.bzl", "pkg_files")
load("//test/docker:source_image_contracts.bzl", "declared_image_archive")
load("//tools:arch.bzl", "select_arch")
load("//tools:defs.bzl", "pkg_tar")

def image_outputs(name, image, image_name):
    """Exposes a Docker-save archive and Make's content-addressed context.

    Args:
      name: Prefix for the archive and context target names.
      image: The image_manifest target.
      image_name: Stable image name relative to images/, such as basic/alpine.
    """
    image_load(
        name = name + "_load",
        image = image,
        tag = "gvisor.dev/images/%s:latest" % image_name,
        tags = ["manual"],
    )
    native.filegroup(
        name = name + "_tarball",
        srcs = [":" + name + "_load"],
        output_group = "tarball",
        tags = ["manual"],
    )
    declared_image_archive(
        name = name + "_archive",
        architecture = select_arch(amd64 = "amd64", arm64 = "arm64"),
        archive = ":" + name + "_tarball",
        image = image_name,
        manifest = image,
        origin = "native-img",
        tags = ["manual"],
        visibility = ["//test/docker:__pkg__"],
    )
    pkg_files(
        name = name + "_files",
        srcs = [":" + name + "_archive"],
        renames = {":" + name + "_archive": "image.tar"},
    )
    pkg_tar(
        name = name + "_context",
        srcs = [":" + name + "_files"],
        package_dir = "images/" + image_name,
        visibility = ["//images:__pkg__"],
    )
