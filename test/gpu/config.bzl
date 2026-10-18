"""Declared GPU/TPU source images and public GPU runtime cohorts."""

# Dockerfile contexts retain images.mk's source hashing. The Ollama client and
# benchmark and Triton images use OCI targets with existing consumer labels.
GPU_SOURCE_IMAGES = {
    "gpu_nccl_source_amd64": "gpu/nccl-tests",
    "gpu_ollama_singlemodel_source_amd64": "gpu/ollama/singlemodel",
    "gpu_ollama_source_amd64": "gpu/ollama",
    "gpu_pytorch_source_amd64": "gpu/pytorch",
    "gpu_sglang_source_amd64": "gpu/sglang",
    "gpu_stable_diffusion_source_amd64": "gpu/stable-diffusion-xl",
    "gpu_vllm_source_amd64": "gpu/vllm",
    "tpu_vllm_source_amd64": "tpu/vllm",
}

GPU_SMOKE_IMAGES = [
    "//test/docker:cuda_samples_source_amd64",
    "//test/docker:cuda_samples_12_8_source_amd64",
]

GPU_ALL_IMAGES = GPU_SMOKE_IMAGES + [
    "//test/docker:gpu_pytorch_source_amd64",
    "//test/docker:gpu_ollama_source_amd64",
    "//test/docker:gpu_ollama_client_source_amd64",
    "//test/docker:gpu_stable_diffusion_source_amd64",
    "//test/docker:gpu_vllm_source_amd64",
    "//test/docker:gpu_nccl_source_amd64",
    "//test/docker:images_basic_busybox_amd64_tar",
    "//test/docker:images_basic_alpine_amd64_tar",
    "//test/docker:images_basic_python_amd64_tar",
    "//test/docker:images_benchmarks_ffmpeg_amd64_tar",
]

GPU_L4_IMAGES = [
    "//test/docker:gpu_sglang_source_amd64",
    "//test/docker:gpu_triton_source_amd64",
    "//test/docker:gpu_ollama_client_source_amd64",
]

# The CUDA12.8 test also probes the supported CUDA version in gpu/cuda-tests.
GPU_CUDA_IMAGES = GPU_SMOKE_IMAGES + ["//test/docker:images_basic_alpine_amd64_tar"]
