load("@rules_oci//oci:defs.bzl", "oci_image")
load("@rules_pkg//pkg:mappings.bzl", "pkg_attributes", "pkg_files")
load("//images:oci.bzl", "image_outputs")
load("//tools:defs.bzl", "go_binary", "pkg_tar")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_binary(
    name = "client",
    srcs = ["client.go"],
    pure = True,
    static = True,
    visibility = ["//images:__pkg__"],
)

pkg_files(
    name = "client_file",
    srcs = [":client"],
    attributes = pkg_attributes(mode = "0755"),
    renames = {":client": "usr/bin/httpclient"},
)

pkg_tar(
    name = "client_layer",
    srcs = [":client_file"],
)

oci_image(
    name = "image",
    base = "//images:helper_base",
    cmd = ["/usr/bin/httpclient"],
    tags = ["manual"],
    # The Go binary follows the target platform and must match the image's OS.
    # This constrains the target, not the host or execution platform.
    target_compatible_with = ["@platforms//os:linux"],
    tars = [":client_layer"],
)

image_outputs(
    name = "image_archive",
    image = ":image",
)
