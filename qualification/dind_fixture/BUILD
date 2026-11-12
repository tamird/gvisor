load("@rules_img//img:image.bzl", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("//images:img.bzl", "image_outputs")

image_layer(
    name = "busybox_archive",
    srcs = {"/docker-images/busybox.tar": "busybox.tar"},
    create_parent_directories = "enabled",
    default_metadata = file_metadata(mode = "0644"),
    include_runfiles = False,
)

image_manifest(
    name = "lightweight",
    base = "@checkpoint_dind_amd64",
    layers = [":busybox_archive"],
    tags = ["manual"],
)

image_outputs(
    name = "lightweight",
    image = ":lightweight",
    image_name = "moby",
)
