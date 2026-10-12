load("@rules_oci//oci:defs.bzl", "oci_image")
load("@rules_pkg//pkg:mappings.bzl", "pkg_attributes", "pkg_files")
load("//images:oci.bzl", "image_outputs")
load("//tools:arch.bzl", "select_arch")
load("//tools:defs.bzl", "go_binary", "pkg_tar")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_binary(
    name = "profilehelper",
    srcs = ["main.go"],
    pure = True,
    static = True,
)

pkg_files(
    name = "helper_file",
    srcs = [":profilehelper"],
    attributes = pkg_attributes(mode = "0755"),
    renames = {":profilehelper": "usr/bin/profilehelper"},
)

pkg_tar(
    name = "helper_layer",
    srcs = [":helper_file"],
)

# Kubernetes invokes these commands directly. GNU rm's --one-file-system
# protects nested mounts under the host directory being removed.
_COREUTILS_MEMBERS = [
    "./bin/chmod",
    "./bin/mkdir",
    "./bin/rm",
    "./lib/{triplet}/libc.so.6",
    "./lib/{triplet}/{loader}",
    "./{interpreter}",
    "./lib/{triplet}/libselinux.so.1",
    "./usr/lib/{triplet}/libpcre2-8.so.0",
    "./usr/lib/{triplet}/libpcre2-8.so.0.10.4",
    "./usr/share/doc/coreutils/copyright",
    "./usr/share/doc/libc6/copyright",
    "./usr/share/doc/libselinux1/copyright",
    "./usr/share/doc/libpcre2-8-0/copyright",
    "./usr/share/doc/base-files/copyright",
    "./usr/share/common-licenses/GPL-2",
    "./usr/share/common-licenses/GPL-3",
    "./usr/share/common-licenses/LGPL-2.1",
    "./usr/share/common-licenses/GFDL-1.3",
]

_COREUTILS_PACKAGES = [
    # Common license texts referenced by the runtime packages' notices.
    "@helper_image_packages//base-files:data",
    # mkdir, chmod and rm --one-file-system invoked by the Kubernetes caller.
    "@helper_image_packages//coreutils:data",
    # C runtime and dynamic loader for those three commands.
    "@helper_image_packages//libc6:data",
    # Shared-library dependency of libselinux.
    "@helper_image_packages//libpcre2-8-0:data",
    # Shared-library dependency of the packaged mkdir binary.
    "@helper_image_packages//libselinux1:data",
]

# bsdtar copies matching entries from the declared package archives, preserving
# their modes and loader links. Listing every required member then rejects an
# incomplete runtime layer if the package contents change.
_COREUTILS_COMMAND = "$(execpath @bsd_tar_toolchains//:resolved_target_toolchain) -cf $@ --format=ustar " + " ".join([
    "--include=" + member
    for member in _COREUTILS_MEMBERS
]) + " " + " ".join([
    "@$(location " + package + ")"
    for package in _COREUTILS_PACKAGES
]) + " && $(execpath @bsd_tar_toolchains//:resolved_target_toolchain) -tf $@ " + " ".join(_COREUTILS_MEMBERS) + " > /dev/null"

genrule(
    name = "coreutils_layer",
    srcs = _COREUTILS_PACKAGES,
    outs = ["coreutils.tar"],
    cmd = select_arch(
        amd64 = _COREUTILS_COMMAND.format(
            interpreter = "lib64/ld-linux-x86-64.so.2",
            loader = "ld-linux-x86-64.so.2",
            triplet = "x86_64-linux-gnu",
        ),
        arm64 = _COREUTILS_COMMAND.format(
            interpreter = "lib/ld-linux-aarch64.so.1",
            loader = "ld-linux-aarch64.so.1",
            triplet = "aarch64-linux-gnu",
        ),
    ),
    tools = ["@bsd_tar_toolchains//:resolved_target_toolchain"],
)

oci_image(
    name = "image",
    base = "//images:helper_base",
    cmd = ["/usr/bin/profilehelper"],
    tags = ["manual"],
    # The Go binary follows the target platform and must match the image's OS.
    # This constrains the target, not the host or execution platform.
    target_compatible_with = ["@platforms//os:linux"],
    tars = [
        ":coreutils_layer",
        ":helper_layer",
    ],
)

image_outputs(
    name = "image_archive",
    image = ":image",
)
