load("@rules_img//img:image.bzl", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("//images:img.bzl", "image_outputs")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

# The integration test checks that mounting /tmp preserves image contents.
image_layer(
    name = "tmpfile",
    srcs = {"/tmp/foo/file.txt": "file.txt"},
    create_parent_directories = "enabled",
    default_metadata = file_metadata(mode = "0644"),
    # Keep the base image's sticky /tmp when adding its child directory.
    file_metadata = {"/tmp": file_metadata(mode = "1777")},
)

image_manifest(
    name = "image",
    base = "@test_alpine",
    layers = [":tmpfile"],
    tags = ["manual"],
)

image_outputs(
    name = "image_archive",
    image = ":image",
)
