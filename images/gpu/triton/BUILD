load("@rules_oci//oci:defs.bzl", "oci_image")
load("@rules_python//python:defs.bzl", "py_binary")
load("//images:oci.bzl", "image_outputs")
load("//tools:defs.bzl", "bzl_library")
load(":assets.bzl", "ENGINE_FILES", "MODEL_FILES")
load(":defs.bzl", "triton_assets", "triton_templates")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

# Keep the separate GPU engine-generation recipe in existing source contexts.
filegroup(
    name = "sources",
    srcs = glob(["**"]),
    visibility = ["//images:__pkg__"],
)

bzl_library(
    name = "assets_bzl",
    srcs = ["assets.bzl"],
)

bzl_library(
    name = "defs_bzl",
    srcs = ["defs.bzl"],
    deps = [
        "//images:layers_bzl",
        "@rules_pkg//pkg:bzl_srcs",
    ],
)

triton_assets(
    name = "model_layer",
    destination = "llama-2-7b-chat-hf",
    files = MODEL_FILES,
    source_prefix = "tests/models/llama-2-7b-chat-hf/",
)

triton_assets(
    name = "engine_layer",
    destination = "models/tensorrt_llm/1",
    files = {path[len("fp8/1-gpu/"):]: metadata for path, metadata in ENGINE_FILES.items()},
    source_prefix = "tests/l4/engines/llama-2-7b-chat-hf/fp8/1-gpu/",
)

filegroup(
    name = "assets",
    srcs = [
        ":engine_layer_assets",
        ":model_layer_assets",
    ],
    tags = ["manual"],
)

py_binary(
    name = "fill_template",
    srcs = ["@triton_template_fill_template//file"],
    tags = ["manual"],
)

triton_templates(name = "templates_layer")

# Archive assembly reads the complete compressed layer set while producing
# a second copy. Reserve space for both the server and the model payload.
_IMAGE_EXEC_PROPERTIES = select({
    "//tools/bazeldefs:rbe": {
        "EstimatedFreeDiskBytes": "200GB",
        "EstimatedMemory": "12GB",
        "workload-isolation-type": "oci",
    },
    "//conditions:default": {},
})

oci_image(
    name = "image",
    base = "@triton_server_linux_amd64",
    cmd = [
        "tritonserver",
        "--model-repository=/models/",
    ],
    exec_properties = _IMAGE_EXEC_PROPERTIES,
    tags = ["manual"],
    tars = [
        ":model_layer",
        ":templates_layer",
        ":engine_layer",
    ],
)

image_outputs(
    name = "image_archive",
    content_tag = True,
    exec_properties = _IMAGE_EXEC_PROPERTIES,
    image = ":image",
)
