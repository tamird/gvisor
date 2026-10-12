"""Shared archive interfaces for images assembled by Bazel."""

load("@rules_oci//oci:defs.bzl", "oci_load")
load("@rules_pkg//pkg:mappings.bzl", "pkg_files")
load("//tools:defs.bzl", "pkg_tar")

def image_outputs(name, image):
    """Exposes one image per images/<path> package to tests and Make.

    Args:
      name: The archive target name.
      image: The package's oci_image target.
    """
    package = native.package_name()
    oci_load(
        name = "load_image",
        format = "docker",
        image = image,
        # Make loads this tag before applying its configured registry tags.
        repo_tags = ["gvisor.dev/%s:latest" % package],
        tags = ["manual"],
    )

    native.filegroup(
        name = name,
        srcs = [":load_image"],
        output_group = "tarball",
        tags = ["manual"],
        visibility = ["//test/docker:__pkg__"],
    )

    pkg_files(
        name = "make_image",
        srcs = [":" + name],
        renames = {":" + name: "image.tar"},
    )

    # Package the complete image archive into Make's hashed context.
    pkg_tar(
        name = "make_context",
        srcs = [":make_image"],
        package_dir = package,
        visibility = ["//images:__pkg__"],
    )
