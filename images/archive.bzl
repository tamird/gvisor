"""Make contexts for images assembled by Bazel."""

load("@rules_pkg//pkg:mappings.bzl", "pkg_files")
load("//tools:defs.bzl", "pkg_tar")

def image_context(archive, exec_properties = {}):
    """Packages the assembled image for Make's content-based naming.

    Args:
      archive: A Docker-save archive.
      exec_properties: Resources for packaging the archive.
    """
    pkg_files(
        name = "make_image",
        srcs = [archive],
        renames = {archive: "image.tar"},
    )
    pkg_tar(
        name = "make_context",
        srcs = [":make_image"],
        exec_properties = exec_properties,
        package_dir = native.package_name(),
        visibility = ["//images:__pkg__"],
    )
