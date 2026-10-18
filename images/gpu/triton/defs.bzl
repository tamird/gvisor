"""Assemble the existing Triton model repository from declared inputs."""

load("@rules_pkg//pkg:mappings.bzl", "pkg_attributes", "pkg_files")
load("//images:layers.bzl", "compressed_layer", "downloaded_file")

def triton_assets(name, files, source_prefix, destination):
    """Downloads a pinned GCS prefix and packages its original file layout.

    Args:
      name: Layer target and prefix for file targets.
      files: Filename to generation, digest and size metadata.
      source_prefix: GCS object prefix in the public gvisor bucket.
      destination: Absolute image directory, without the leading slash.
    """
    labels = []
    renames = {}
    raw_size = 0
    for path, metadata in files.items():
        target = name + "_" + path.replace("/", "_")
        downloaded_file(
            name = target,
            url = "https://storage.googleapis.com/gvisor/" + source_prefix + path + "?generation=" + metadata.generation,
            sha256 = metadata.sha256,
            size_bytes = metadata.size,
        )
        label = ":" + target
        labels.append(label)
        renames[label] = destination + "/" + path
        raw_size += metadata.size
    native.filegroup(
        name = name + "_assets",
        srcs = labels,
        tags = ["manual"],
    )
    pkg_files(
        name = name + "_files",
        srcs = labels,
        attributes = pkg_attributes(mode = "0644"),
        renames = renames,
    )
    compressed_layer(
        name = name,
        srcs = [":" + name + "_files"],
        raw_size = raw_size,
    )

def triton_templates(name):
    """Fills the five original configs with the pinned upstream script."""
    substitutions = {
        "preprocessing": "tokenizer_dir:/llama-2-7b-chat-hf,triton_max_batch_size:1,preprocessing_instance_count:1",
        "postprocessing": "tokenizer_dir:/llama-2-7b-chat-hf,triton_max_batch_size:1,postprocessing_instance_count:1",
        "tensorrt_llm_bls": "prompt_embedding_table_data_type:TYPE_FP16,triton_max_batch_size:1,decoupled_mode:true,bls_instance_count:1,logits_datatype:TYPE_FP32",
        "ensemble": "triton_max_batch_size:1,logits_datatype:TYPE_FP32",
        "tensorrt_llm": "prompt_embedding_table_data_type:TYPE_FP16,triton_backend:tensorrtllm,triton_max_batch_size:1,decoupled_mode:true,engine_dir:/models//tensorrt_llm/1,max_queue_delay_microseconds:10000,batching_strategy:inflight_fused_batching,encoder_input_features_data_type:TYPE_FP16,logits_datatype:TYPE_FP32",
    }
    configs = []
    for model, values in substitutions.items():
        source = "@triton_template_" + model + "_config//file"
        target = name + "_" + model
        native.genrule(
            name = target,
            srcs = [source],
            outs = [model + "/config.pbtxt"],
            tools = [":fill_template"],
            cmd = "cp $(location %s) $@ && $(location :fill_template) -i $@ '%s'" % (source, values),
            tags = ["manual"],
        )
        pkg_files(
            name = target + "_file",
            srcs = [":" + target],
            attributes = pkg_attributes(mode = "0755" if model == "preprocessing" else "0644"),
            renames = {":" + target: "models/" + model + "/config.pbtxt"},
        )
        configs.append(":" + target + "_file")
    pkg_files(
        name = name + "_script",
        srcs = ["@triton_template_fill_template//file"],
        attributes = pkg_attributes(mode = "0644"),
        renames = {"@triton_template_fill_template//file": "usr/local/bin/fill_template.py"},
    )

    # Preserve the executable bits from the original Git checkout.
    model_files = []
    for group, mode, paths in [
        ("models", "0644", {
            "ensemble/1/.tmp": "@triton_template_ensemble_placeholder//file",
            "postprocessing/1/model.py": "@triton_template_postprocessing_model//file",
            "tensorrt_llm_bls/1/lib/decode.py": "@triton_template_tensorrt_llm_bls_decode//file",
            "tensorrt_llm_bls/1/lib/triton_decoder.py": "@triton_template_tensorrt_llm_bls_triton_decoder//file",
            "tensorrt_llm_bls/1/model.py": "@triton_template_tensorrt_llm_bls_model//file",
        }),
        ("executables", "0755", {
            "preprocessing/1/model.py": "@triton_template_preprocessing_model//file",
            "tensorrt_llm/1/model.py": "@triton_template_tensorrt_llm_model//file",
        }),
    ]:
        target = name + "_" + group
        pkg_files(
            name = target,
            srcs = paths.values(),
            attributes = pkg_attributes(mode = mode),
            renames = {label: "models/" + path for path, label in paths.items()},
        )
        model_files.append(":" + target)
    compressed_layer(
        name = name,
        srcs = configs + model_files + [":" + name + "_script"],
        raw_size = 250000,
    )
