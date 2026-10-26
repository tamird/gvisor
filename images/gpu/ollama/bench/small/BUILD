load("//images:oci.bzl", "image_outputs")
load("//images/gpu/ollama/bench:layers.bzl", "model_image")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

_IMAGE_EXEC_PROPERTIES = select({
    "//tools/bazeldefs:rbe": {
        "EstimatedFreeDiskBytes": "80GB",
        "EstimatedMemory": "12GB",
        "workload-isolation-type": "oci",
    },
    "//conditions:default": {},
})

# The smallest existing benchmark model exercises the same download, package,
# archive and server paths before qualification selects the full model set.
model_image(
    name = "image",
    exec_properties = _IMAGE_EXEC_PROPERTIES,
    layers = ["//images/gpu/ollama/bench:snowflake-arctic-embed2_568m-l-fp16_layer"],
)

image_outputs(
    name = "image_archive",
    architecture = "amd64",
    content_tag = True,
    exec_properties = _IMAGE_EXEC_PROPERTIES,
    image = ":image",
)
