load("@rules_img//img:image.bzl", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("//images:img.bzl", "image_outputs")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

filegroup(
    name = "sources",
    srcs = glob(["**"]),
    visibility = ["//images:__pkg__"],
)

image_layer(
    name = "server_layer",
    srcs = {"/server.rb": "server.rb"},
    default_metadata = file_metadata(mode = "0644"),
)

image_manifest(
    name = "ruby",
    base = "@ruby_http_debian",
    cmd = [
        "/usr/bin/ruby3.3",
        "/server.rb",
    ],
    layers = [
        "@ruby_http_packages//:flat",
        ":server_layer",
    ],
    tags = ["manual"],
)

image_outputs(
    name = "ruby",
    image = ":ruby",
    image_name = "image-test/ruby",
)
