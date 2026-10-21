load("@rules_img//img:image.bzl", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("//images:img.bzl", "image_outputs")
load("//tools:defs.bzl", "go_binary")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_binary(
    name = "client",
    srcs = ["client/client.go"],
    pure = True,
    static = True,
)

image_layer(
    name = "client_layer",
    srcs = {"/usr/bin/httpclient": ":client"},
    default_metadata = file_metadata(mode = "0755"),
)

image_manifest(
    name = "client_image",
    base = "//images:helper_base",
    cmd = ["/usr/bin/httpclient"],
    layers = [":client_layer"],
    tags = ["manual"],
    # The Go binary follows the target platform and must match the image's OS.
    # This constrains the target, not the host or execution platform.
    target_compatible_with = ["@platforms//os:linux"],
)

image_outputs(
    name = "client",
    image = ":client_image",
    image_name = "gpu/ollama/client",
)
