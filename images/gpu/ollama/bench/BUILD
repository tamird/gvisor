load("@ollama_models//:models.bzl", "MODELS")
load("//images:oci.bzl", "image_outputs")
load("//tools:defs.bzl", "bzl_library")
load(":layers.bzl", "model_image", "model_layer_name", "model_layers")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

exports_files(glob(["manifests/*.json"]))

bzl_library(
    name = "repositories_bzl",
    srcs = ["repositories.bzl"],
)

bzl_library(
    name = "layers_bzl",
    srcs = ["layers.bzl"],
    visibility = ["//images/gpu/ollama/bench:__subpackages__"],
    deps = [
        "//images:layers_bzl",
        "@rules_oci//oci:defs",
        "@rules_pkg//pkg:bzl_srcs",
    ],
)

model_layers(
    name = "models",
    models = MODELS,
)

_IMAGE_EXEC_PROPERTIES = select({
    "//tools/bazeldefs:rbe": {
        "EstimatedFreeDiskBytes": "200GB",
        "EstimatedMemory": "12GB",
        "workload-isolation-type": "oci",
    },
    "//conditions:default": {},
})

model_image(
    name = "image",
    exec_properties = _IMAGE_EXEC_PROPERTIES,
    layers = [":" + model_layer_name(model) for model in MODELS],
)

image_outputs(
    name = "image_archive",
    content_tag = True,
    exec_properties = _IMAGE_EXEC_PROPERTIES,
    image = ":image",
)
