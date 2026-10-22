"""Test image archives and contexts for the Make image targets."""

load("@rules_img//img:load.bzl", "image_load")
load("@rules_pkg//pkg:mappings.bzl", "pkg_files")
load("//tools:arch.bzl", "select_arch")
load("//tools:defs.bzl", "pkg_tar")

def package_layer(name, packages, members, out):
    """Copies selected runtime files and notices from declared Debian packages.

    Args:
      name: Name of the layer target.
      packages: Package data archives containing the selected files.
      members: Required archive paths, with optional {triplet}, {loader} and
        {interpreter} placeholders for architecture-specific library paths.
      out: Output tar filename.
    """

    # Preserve package modes and symlinks, and reject missing runtime files.
    tar = "$(execpath @bsd_tar_toolchains//:resolved_target_toolchain)"
    command = tar + " -cf $@ --format=ustar " + " ".join([
        "--include=" + member
        for member in members
    ]) + " " + " ".join([
        "@$(location " + package + ")"
        for package in packages
    ]) + " && " + tar + " -tf $@ " + " ".join(members) + " > /dev/null"
    native.genrule(
        name = name,
        srcs = packages,
        outs = [out],
        cmd = select_arch(
            amd64 = command.format(
                interpreter = "lib64/ld-linux-x86-64.so.2",
                loader = "ld-linux-x86-64.so.2",
                triplet = "x86_64-linux-gnu",
            ),
            arm64 = command.format(
                interpreter = "lib/ld-linux-aarch64.so.1",
                loader = "ld-linux-aarch64.so.1",
                triplet = "aarch64-linux-gnu",
            ),
        ),
        tools = ["@bsd_tar_toolchains//:resolved_target_toolchain"],
    )

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
        name = name + "_archive",
        srcs = [":" + name + "_load"],
        output_group = "tarball",
        tags = ["manual"],
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
    )
