load("@rules_img//img:image.bzl", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("@rules_pkg//pkg:mappings.bzl", "pkg_attributes", "pkg_files")
load("//images:img.bzl", "image_outputs")
load("//tools:defs.bzl", "go_binary", "pkg_tar")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

exports_files(
    [
        "benchmarks/node/index.js",
        "benchmarks/node/index_test.js",
    ],
    visibility = ["//test/benchmarks/network:__pkg__"],
)

# Include the complete contexts, including files owned by nested packages.
# tools/images.mk remains responsible for architecture selection and hashing.
filegroup(
    name = "sources",
    srcs = glob(["**"]) + [
        "//images/basic:sources",
        "//images/benchmarks/bun-spawn:sources",
        "//images/benchmarks/hey:sources",
        "//images/compatibility:sources",
        "//images/gpu/ollama/client:sources",
    ],
)

# Build the CUDA sample runners with the repository Go toolchain.
# Their former Docker build used CGO_ENABLED=0; preserve that link contract.
go_binary(
    name = "cuda_run_sample",
    srcs = ["gpu/cuda-tests/run_sample.go"],  # keep
    pure = True,
    static = True,
)

go_binary(
    name = "cuda_12_8_run_sample",
    srcs = ["gpu/cuda-tests-12-8/run_sample.go"],  # keep
    pure = True,
    static = True,
)

pkg_files(
    name = "image_programs",
    srcs = [
        ":cuda_12_8_run_sample",
        ":cuda_run_sample",
        "//images/gpu/ollama/client",
    ],
    attributes = pkg_attributes(mode = "0755"),
    renames = {
        ":cuda_run_sample": "gpu/cuda-tests/run_sample",
        ":cuda_12_8_run_sample": "gpu/cuda-tests-12-8/run_sample",
        "//images/gpu/ollama/client": "gpu/ollama/client/client",
    },
)

# Bazel uploads every remote input as executable. Archive metadata preserves
# context modes independently of that input-tree normalization. Keep these
# overrides aligned with tracked executable files when adding context sources.
# https://github.com/bazelbuild/bazel/blob/d84820503/src/main/java/com/google/devtools/build/lib/remote/merkletree/MerkleTree.java#L400
pkg_tar(
    name = "source_contexts",
    srcs = [
        ":image_programs",
        ":sources",
    ],
    mode = "0644",
    modes = {
        "arm-qemu/initramfs/init": "0755",
        "arm-qemu/test.sh": "0755",
        "basic/docker/start-dockerd.sh": "0755",
        "basic/fsstress/run.sh": "0755",
        "benchmarks/ruby/Dockerfile": "0755",
        "benchmarks/ruby/Gemfile": "0755",
        "benchmarks/ruby/config.ru": "0755",
        "benchmarks/ruby/index.erb": "0755",
        "benchmarks/ruby/main.rb": "0755",
        "fuse/gcs/run_test.sh": "0755",
        "gpu/cuda-tests-12-8/run_smoke.sh": "0755",
        "gpu/cuda-tests/run_smoke.sh": "0755",
        "gpu/ollama/bench/pull.sh": "0755",
        "jekyll/build.sh": "0755",
    },
    package_dir = "images",
    strip_prefix = ".",
)

image_outputs(
    name = "benchmark_alpine",
    archive_visibility = ["//test/benchmarks/harness:__pkg__"],
    image = select({
        "@platforms//cpu:aarch64": "@benchmark_alpine_arm64",
        "@platforms//cpu:x86_64": "@benchmark_alpine_amd64",
    }),
    image_name = "benchmarks/alpine",
)

image_outputs(
    name = "benchmark_node",
    archive_visibility = ["//test/benchmarks/network:__pkg__"],
    image = "@benchmark_node_amd64",
    image_name = "benchmarks/node",
)

image_layer(
    name = "benchmark_node_completion_layer",
    srcs = {"/usr/src/app/index.js": "benchmarks/node/index.js"},
    create_parent_directories = "enabled",
    default_metadata = file_metadata(mode = "0644"),
    include_runfiles = False,
)

image_manifest(
    name = "benchmark_node_completed",
    base = "@benchmark_node_amd64",
    layers = [":benchmark_node_completion_layer"],
    tags = ["manual"],
)

image_outputs(
    name = "benchmark_node_completed",
    archive_visibility = ["//test/benchmarks/network:__pkg__"],
    image = ":benchmark_node_completed",
    image_name = "benchmarks/node",
)

image_outputs(
    name = "benchmark_hey",
    archive_visibility = ["//test/benchmarks/network:__pkg__"],
    image = "@benchmark_hey_amd64",
    image_name = "benchmarks/hey",
)

image_outputs(
    name = "benchmark_util",
    archive_visibility = ["//test/benchmarks/network:__pkg__"],
    image = "@benchmark_util_amd64",
    image_name = "benchmarks/util",
)
