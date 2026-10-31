load("@rules_img//img:image.bzl", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("//images:img.bzl", "image_outputs")
load("//tools:defs.bzl", "go_binary")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

go_binary(
    name = "hey",
    embed = ["@com_github_rakyll_hey//:go_default_library"],
    pure = True,
    static = True,
)

image_layer(
    name = "hey_layer",
    srcs = {"/bin/hey": ":hey"},
    default_metadata = file_metadata(mode = "0755"),
    include_runfiles = False,
)

image_manifest(
    name = "hey_image",
    cmd = ["/bin/hey"],
    env = {"PATH": "/bin"},
    layers = [":hey_layer"],
    tags = ["manual"],
    # The target OS selects the executable format installed in this Linux image.
    target_compatible_with = ["@platforms//os:linux"],
)

image_outputs(
    name = "hey",
    image = ":hey_image",
    image_name = "benchmarks/hey",
)
