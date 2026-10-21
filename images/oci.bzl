"""Shared archive interfaces for images assembled by Bazel."""

load("@rules_oci//oci:defs.bzl", "oci_load")
load("//test/docker:source_image_contracts.bzl", "declared_image_archive")
load("//tools:arch.bzl", "select_arch")
load(":archive.bzl", "image_context")

def image_outputs(name, image, content_tag = False, exec_properties = {}, architecture = None):
    """Exposes one image per images/<path> package to tests and Make.

    Args:
      name: The archive target name.
      image: The package's oci_image target.
      content_tag: Include the immutable image digest tag before latest.
      exec_properties: Resources for materializing the Docker archive.
      architecture: Fixed image architecture, or None for the configured CPU.
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
        name = name + "_tarball",
        srcs = [":load_image"],
        output_group = "tarball",
        tags = ["manual"],
    )

    # Make contexts and Docker consumers share this configured identity.
    declared_image_archive(
        name = name,
        archive = ":" + name + "_tarball",
        architecture = architecture if architecture != None else select_arch(amd64 = "amd64", arm64 = "arm64"),
        image = package.removeprefix("images/"),
        origin = "native-oci",
        layout = image,
        tags = ["manual"],
        visibility = ["//test/docker:__pkg__"],
    )

    image_context(archive = ":" + name, exec_properties = exec_properties)
