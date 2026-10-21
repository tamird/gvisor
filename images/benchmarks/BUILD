load("@rules_img//img:image.bzl", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("//images:img.bzl", "image_outputs")
load("//images:package_layer.bzl", "package_layer")
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
package_layer(
    name = "profile_helper_coreutils_layer",
    members = [
        "./bin/chmod",
        "./bin/mkdir",
        "./bin/rm",
    ],
    packages = ["@helper_image_packages//coreutils:data"],
)

# mkdir also needs SELinux and its PCRE dependency.
package_layer(
    name = "profile_helper_selinux_layer",
    members = [
        "./lib/{triplet}/libselinux.so.1",
        "./usr/lib/{triplet}/libpcre2-8.so.0",
        "./usr/lib/{triplet}/libpcre2-8.so.0.10.4",
        "./usr/share/doc/libselinux1/copyright",
        "./usr/share/doc/libpcre2-8-0/copyright",
    ],
    packages = [
        # Shared-library dependency of libselinux.
        "@helper_image_packages//libpcre2-8-0:data",
        # Shared-library dependency of the packaged mkdir binary.
        "@helper_image_packages//libselinux1:data",
    ],
)

image_manifest(
    name = "profile_helper_image",
    base = "//images:helper_base",
    cmd = ["/usr/bin/profilehelper"],
    layers = [
        "//images:helper_coreutils_runtime",
        ":profile_helper_coreutils_layer",
        ":profile_helper_selinux_layer",
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

# Keep the rt-tests version and command-line behavior used by the benchmark.
cc_binary(
    name = "hackbench",
    srcs = ["@rt_tests//:src/hackbench/hackbench.c"],
    copts = ["-O2"],
    defines = [
        "VERSION=2.2",
        "_GNU_SOURCE",
    ],
    linkopts = ["-lrt"],
    static = True,
    # The executable uses Linux IPC and scheduling interfaces.
    target_compatible_with = ["@platforms//os:linux"],
)

image_layer(
    name = "hackbench_layer",
    srcs = {
        "/usr/bin/hackbench": ":hackbench",
        "/usr/share/doc/hackbench/COPYING": "@rt_tests//:COPYING",
    },
    create_parent_directories = "enabled",
    default_metadata = file_metadata(mode = "0644"),
    file_metadata = {"/usr/bin/hackbench": file_metadata(mode = "0755")},
)

# The benchmark starts sleep before executing hackbench repeatedly.
package_layer(
    name = "hackbench_coreutils_layer",
    members = ["./bin/sleep"],
    packages = ["@helper_image_packages//coreutils:data"],
)

image_manifest(
    name = "hackbench_image",
    base = "//images:helper_base",
    cmd = ["/usr/bin/hackbench"],
    layers = [
        "//images:helper_coreutils_runtime",
        ":hackbench_coreutils_layer",
        ":hackbench_layer",
    ],
    tags = ["manual"],
    # Match the Linux executable and selected image platform, not the build host.
    target_compatible_with = ["@platforms//os:linux"],
)

image_outputs(
    name = "hackbench",
    image = ":hackbench_image",
    image_name = "benchmarks/hackbench",
)

cc_binary(
    name = "iperf",
    srcs = [
        "iperf/config.h",
        "@iperf//:sources",
    ],
    copts = ["-O2"],
    defines = [
        "HAVE_CONFIG_H",
        "_GNU_SOURCE",
    ],
    includes = ["iperf"],
    linkopts = [
        "-lm",
        "-lrt",
    ],
    static = True,
    # config.h describes the Linux interfaces used by both supported CPUs.
    target_compatible_with = ["@platforms//os:linux"],
    deps = ["@iperf//:headers"],
)

image_layer(
    name = "iperf_layer",
    srcs = {
        "/usr/bin/iperf": ":iperf",
        "/usr/share/doc/iperf/COPYING": "@iperf//:COPYING",
        "/usr/share/doc/iperf/NOTICE": "iperf/NOTICE",
    },
    create_parent_directories = "enabled",
    default_metadata = file_metadata(mode = "0644"),
    file_metadata = {"/usr/bin/iperf": file_metadata(mode = "0755")},
)

# The bundled getopt is LGPL-2-or-later; retain its full LGPL 2.1 license
# alongside the upstream notices, using the already-declared package.
package_layer(
    name = "iperf_licenses_layer",
    members = [
        "./usr/share/common-licenses/GPL-2",
        "./usr/share/common-licenses/LGPL-2.1",
        "./usr/share/doc/base-files/copyright",
    ],
    packages = ["@helper_image_packages//base-files:data"],
)

image_manifest(
    name = "iperf_image",
    base = "//images:helper_base",
    cmd = ["/usr/bin/iperf"],
    layers = [
        ":iperf_licenses_layer",
        ":iperf_layer",
    ],
    tags = ["manual"],
    # Match the executable's Linux platform, independently of the build host.
    target_compatible_with = ["@platforms//os:linux"],
)

image_outputs(
    name = "iperf",
    image = ":iperf_image",
    image_name = "benchmarks/iperf",
)
