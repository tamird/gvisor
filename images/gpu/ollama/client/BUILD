load("@rules_oci//oci:defs.bzl", "oci_image", "oci_load")
load("@rules_pkg//pkg:mappings.bzl", "pkg_attributes", "pkg_files")
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
    base = "@ollama_client_alpine",
    cmd = ["/usr/bin/httpclient"],
    tags = ["manual"],
    target_compatible_with = ["@platforms//os:linux"],
    tars = [":client_layer"],
)

oci_load(
    name = "load_image",
    format = "docker",
    image = ":image",
    # Make also loads this tag before applying its configured registry tags.
    repo_tags = ["gvisor.dev/images/gpu/ollama/client:latest"],
    tags = ["manual"],
)

filegroup(
    name = "image_archive",
    srcs = [":load_image"],
    output_group = "tarball",
    tags = ["manual"],
    visibility = ["//test/docker:__pkg__"],
)

pkg_files(
    name = "make_image",
    srcs = [":image_archive"],
    renames = {":image_archive": "image.tar"},
)

# Hash the assembled image, including its base, instead of a Dockerfile context.
pkg_tar(
    name = "make_context",
    srcs = [":make_image"],
    package_dir = "images/gpu/ollama/client",
    visibility = ["//images:__pkg__"],
)
