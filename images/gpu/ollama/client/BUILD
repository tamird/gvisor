load("@rules_oci//oci:defs.bzl", "oci_image", "oci_load")
load("@rules_pkg//pkg:mappings.bzl", "pkg_attributes", "pkg_files")
load("//tools:defs.bzl", "go_binary", "pkg_tar")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

filegroup(
    name = "sources",
    srcs = glob(["**"]),
    visibility = ["//images:__pkg__"],
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

# The Dockerfile only copies this executable and sets its default command.
oci_image(
    name = "image",
    base = "@ollama_client_alpine_linux_amd64",
    cmd = ["/usr/bin/httpclient"],
    tags = ["manual"],
    target_compatible_with = [
        "@platforms//os:linux",
        "@platforms//cpu:x86_64",
    ],
    tars = [":client_layer"],
)

oci_load(
    name = "load_image",
    format = "docker",
    image = ":image",
    # The declared archive supplies the tag used by the runtime tests.
    # Bazel owns its cache identity; this does not publish Make's registry tags.
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
