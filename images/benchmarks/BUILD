load("@rules_img//img:image.bzl", "image_index", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("//images:img.bzl", "image_outputs", "package_layer")
load("//tools:defs.bzl", "cc_binary", "go_binary")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

cc_binary(
    name = "syscallbench",
    srcs = ["syscallbench.c"],
    copts = ["-O2"],
    linkopts = ["-static"],
    # The assembly loop and seccomp filters use the Linux x86-64 ABI.
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
)

image_layer(
    name = "syscallbench_layer",
    srcs = {"/usr/bin/syscallbench": ":syscallbench"},
    default_metadata = file_metadata(mode = "0755"),
    # The benchmark keeps the container alive while executing each mode.
    symlinks = {
        "/bin/sh": "/bin/busybox",
        "/bin/sleep": "/bin/busybox",
    },
)

image_manifest(
    name = "syscallbench_manifest",
    env = {"PATH": "/usr/bin:/bin"},
    layers = [
        "//images:busybox_layer",
        "//images:musl_notice_layer",
        ":syscallbench_layer",
    ],
    tags = ["manual"],
    user = "0:0",
)

# The existing benchmark assembly supports only the Linux x86-64 target.
image_index(
    name = "syscallbench_image",
    manifests = [":syscallbench_manifest"],
    platforms = ["@llvm//platforms:linux_x86_64_musl"],
    tags = ["manual"],
    target_compatible_with = ["@platforms//cpu:x86_64"],
)

image_outputs(
    name = "syscallbench",
    image = ":syscallbench_image",
    image_name = "benchmarks/syscallbench",
)

go_binary(
    name = "profilehelper",
    srcs = ["profile-helper/main.go"],
    pure = True,
    static = True,
)

image_layer(
    name = "profile_helper_layer",
    srcs = {"/usr/bin/profilehelper": ":profilehelper"},
    default_metadata = file_metadata(mode = "0755"),
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

package_layer(
    name = "profile_helper_coreutils_layer",
    out = "profile-helper-coreutils.tar",
    members = _COREUTILS_MEMBERS,
    packages = _COREUTILS_PACKAGES,
)

image_manifest(
    name = "profile_helper_image",
    base = "//images:helper_base",
    cmd = ["/usr/bin/profilehelper"],
    layers = [
        ":profile_helper_coreutils_layer",
        ":profile_helper_layer",
    ],
    tags = ["manual"],
    # The Go binary follows the target platform and must match the image's OS.
    # This constrains the target, not the host or execution platform.
    target_compatible_with = ["@platforms//os:linux"],
)

image_outputs(
    name = "profile_helper",
    image = ":profile_helper_image",
    image_name = "benchmarks/profile-helper",
)
