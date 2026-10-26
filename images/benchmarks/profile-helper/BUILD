load("@rules_oci//oci:defs.bzl", "oci_image")
load("@rules_pkg//pkg:mappings.bzl", "pkg_attributes", "pkg_files")
load("//images:oci.bzl", "image_outputs")
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

# The Kubernetes profiling helper also executes mkdir, chmod and GNU rm.
oci_image(
    name = "image",
    base = "@profile_helper_ubuntu",
    tags = ["manual"],
    target_compatible_with = ["@platforms//os:linux"],
    tars = [":helper_layer"],
)

image_outputs(
    name = "image_archive",
    image = ":image",
)
