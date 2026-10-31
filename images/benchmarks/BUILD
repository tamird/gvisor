load("@rules_img//img:image.bzl", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("//images:img.bzl", "image_outputs")
load("//tools:arch.bzl", "select_arch")
load("//tools:defs.bzl", "cc_binary", "go_binary")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

cc_binary(
    name = "syscallbench",
    srcs = ["syscallbench.c"],
    copts = ["-O2"],
    static = True,
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
)

image_manifest(
    name = "syscallbench_image",
    # The benchmark starts sleep before executing syscallbench repeatedly.
    base = "@test_busybox",
    layers = [":syscallbench_layer"],
    tags = ["manual"],
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
    name = "profile_helper_coreutils_layer",
    srcs = _COREUTILS_PACKAGES,
    outs = ["profile-helper-coreutils.tar"],
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

image_layer(
    name = "iperf_layer",
    srcs = {
        "/usr/bin/iperf3": "@iperf//:iperf3",
        "/usr/share/doc/iperf3/LICENSE": "@iperf//:LICENSE",
        "/usr/share/doc/openssl/LICENSE.txt": "@openssl//:LICENSE.txt",
    },
    create_parent_directories = "enabled",
    default_metadata = file_metadata(mode = "0644"),
    file_metadata = {"/usr/bin/iperf3": file_metadata(mode = "0755")},
)

image_manifest(
    name = "iperf_image",
    base = "//images:helper_base",
    cmd = ["/usr/bin/iperf3"],
    layers = [":iperf_layer"],
    tags = ["manual"],
    # Match the executable's Linux platform, independently of the build host.
    target_compatible_with = ["@platforms//os:linux"],
)

image_outputs(
    name = "iperf",
    image = ":iperf_image",
    image_name = "benchmarks/iperf",
)
