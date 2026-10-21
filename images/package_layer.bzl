"""Selected files from the declared image runtime packages."""

load("//tools:arch.bzl", "select_arch")

def package_layer(name, packages, members, visibility = None):
    """Copies required package members, preserving their modes and links.

    Args:
      name: Layer target name.
      packages: Declared package data archives.
      members: Required tar paths, optionally containing {triplet}, {loader},
        or {interpreter} for the selected architecture.
      visibility: Target visibility.
    """

    # Listing every required member rejects an incomplete runtime layer if the
    # package contents change. Both operations use the declared bsdtar tool.
    command = "$(execpath @bsd_tar_toolchains//:resolved_target_toolchain) -cf $@ --format=ustar " + " ".join([
        "--include=" + member
        for member in members
    ]) + " " + " ".join([
        "@$(location " + package + ")"
        for package in packages
    ]) + " && $(execpath @bsd_tar_toolchains//:resolved_target_toolchain) -tf $@ " + " ".join(members) + " > /dev/null"
    native.genrule(
        name = name,
        srcs = packages,
        outs = [name + ".tar"],
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
        visibility = visibility,
    )
