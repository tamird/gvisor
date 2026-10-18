"""Shared archive interfaces for images assembled by Bazel."""

load("@rules_oci//oci:defs.bzl", "oci_load")
load("@rules_pkg//pkg:mappings.bzl", "pkg_files")
load("//tools:defs.bzl", "pkg_tar")

def image_outputs(name, image, content_tag = False, exec_properties = {}):
    """Exposes one image per images/<path> package to tests and Make.

    Args:
      name: The archive target name.
      image: The package's oci_image target.
      content_tag: Include the immutable image digest tag before latest.
      exec_properties: Resources for materializing the Docker archive.
    """
    package = native.package_name()
    repo_tags = ["gvisor.dev/%s:latest" % package]
    if content_tag:
        native.genrule(
            name = "image_tags",
            srcs = [image + ".digest"],
            outs = ["image_tags.txt"],
            cmd = 'read -r digest < $(location %s.digest); printf "gvisor.dev/%s:%%s\\ngvisor.dev/%s:latest\\n" "$${digest#sha256:}" > "$@"' % (image, package, package),
            tags = ["manual"],
        )
        repo_tags = ":image_tags"
    oci_load(
        name = "load_image",
        exec_properties = exec_properties,
        format = "docker",
        image = image,
        # Make loads this tag before applying its configured registry tags.
        repo_tags = repo_tags,
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

    # Hash the assembled image, including its base, instead of a Dockerfile.
    pkg_tar(
        name = "make_context",
        srcs = [":make_image"],
        exec_properties = exec_properties,
        package_dir = package,
        visibility = ["//images:__pkg__"],
    )
