load("@rules_img//img:image.bzl", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("//images:img.bzl", "image_outputs")
load("//tools:defs.bzl", "go_binary")

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

image_layer(
    name = "client_layer",
    srcs = {"/usr/bin/httpclient": ":client"},
    default_metadata = file_metadata(mode = "0755"),
    include_runfiles = False,
)

image_manifest(
    name = "client_image",
    base = "//images:alpine_base",
    cmd = ["/usr/bin/httpclient"],
    layers = [":client_layer"],
    tags = ["manual"],
    # The target OS selects the executable format installed in this Linux image.
    target_compatible_with = ["@platforms//os:linux"],
)

image_outputs(
    name = "client",
    image = ":client_image",
    image_name = "gpu/ollama/client",
)
