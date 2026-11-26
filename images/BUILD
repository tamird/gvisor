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
    ["benchmarks/absl/Dockerfile.x86_64"],
    visibility = ["//test/benchmarks/fs:__pkg__"],
)

# Include the complete contexts, including files owned by nested packages.
# tools/images.mk remains responsible for architecture selection and hashing.
filegroup(
    name = "sources",
    srcs = glob(["**"]) + [
        "//images/basic:sources",
        "//images/benchmarks/bun-spawn:sources",
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
    image = select({
        "@platforms//cpu:aarch64": "@benchmark_alpine_arm64",
        "@platforms//cpu:x86_64": "@benchmark_alpine_amd64",
    }),
    image_name = "benchmarks/alpine",
    visibility = ["//test/benchmarks/harness:__pkg__"],
)

image_outputs(
    name = "benchmark_nginx",
    image = "@benchmark_nginx_amd64",
    image_name = "benchmarks/nginx",
    visibility = ["//test/benchmarks/harness:__pkg__"],
)

image_outputs(
    name = "benchmark_node",
    image = "@benchmark_node_amd64",
    image_name = "benchmarks/node",
    visibility = ["//test/benchmarks/harness:__pkg__"],
)

image_outputs(
    name = "benchmark_util",
    image = "@benchmark_util_amd64",
    image_name = "benchmarks/util",
    visibility = ["//test/benchmarks/harness:__pkg__"],
)

image_outputs(
    name = "benchmark_iperf",
    image = "@benchmark_iperf_amd64",
    image_name = "benchmarks/iperf",
    visibility = ["//test/benchmarks/harness:__pkg__"],
)

image_outputs(
    name = "benchmark_httpd",
    image = "@benchmark_httpd_amd64",
    image_name = "benchmarks/httpd",
    visibility = ["//test/benchmarks/harness:__pkg__"],
)

image_outputs(
    name = "benchmark_ruby",
    image = "@benchmark_ruby_amd64",
    image_name = "benchmarks/ruby",
    visibility = ["//test/benchmarks/harness:__pkg__"],
)

image_outputs(
    name = "benchmark_hey",
    image = "@benchmark_hey_amd64",
    image_name = "benchmarks/hey",
    visibility = ["//test/benchmarks/harness:__pkg__"],
)

image_outputs(
    name = "benchmark_sysbench",
    image = "@benchmark_sysbench_amd64",
    image_name = "benchmarks/sysbench",
    visibility = ["//test/benchmarks/harness:__pkg__"],
)

image_outputs(
    name = "benchmark_fio",
    image = "@benchmark_fio_amd64",
    image_name = "benchmarks/fio",
    visibility = ["//test/benchmarks/harness:__pkg__"],
)

image_outputs(
    name = "benchmark_absl",
    image = "@benchmark_absl_amd64",
    image_name = "benchmarks/absl",
    visibility = ["//test/benchmarks/harness:__pkg__"],
)

image_outputs(
    name = "benchmark_build_grpc",
    image = "@benchmark_build_grpc_amd64",
    image_name = "benchmarks/build-grpc",
    visibility = ["//test/benchmarks/harness:__pkg__"],
)

genrule(
    name = "rubydev_fixture_content_check",
    srcs = [
        "@fastlane_benchmark_fixture//:match/spec/encryption/openssl_spec.rb",
        "@fastlane_benchmark_fixture//:spaceship/lib/spaceship/connect_api/token.rb",
        "@fastlane_benchmark_fixture//:spaceship/spec/connect_api/token_spec.rb",
    ],
    outs = ["rubydev_fixture_content_checked"],
    cmd = "echo '202cab0d0c85b1ce14e923ef115c6d709c909168998f2cce34fa2bf5ad8b568c  $(location @fastlane_benchmark_fixture//:match/spec/encryption/openssl_spec.rb)\n" +
          "72344d9f37b888e1e6717400e412e8c19189cdaaee599d45c3e9bf8c3ee2c1a5  $(location @fastlane_benchmark_fixture//:spaceship/lib/spaceship/connect_api/token.rb)\n" +
          "fc78ac05618eba99103fada579e00bf057872164f02e44ebf5c30985b67368b5  $(location @fastlane_benchmark_fixture//:spaceship/spec/connect_api/token_spec.rb)' | sha256sum -c - && touch $@",
    tags = ["manual"],
)

image_layer(
    name = "rubydev_fixture_layer",
    srcs = {
        "/fastlane/match/spec/encryption/openssl_spec.rb": "@fastlane_benchmark_fixture//:match/spec/encryption/openssl_spec.rb",
        "/fastlane/spaceship/lib/spaceship/connect_api/token.rb": "@fastlane_benchmark_fixture//:spaceship/lib/spaceship/connect_api/token.rb",
        "/fastlane/spaceship/spec/connect_api/token_spec.rb": "@fastlane_benchmark_fixture//:spaceship/spec/connect_api/token_spec.rb",
    },
    default_metadata = file_metadata(mode = "0644"),
    include_runfiles = False,
)

image_manifest(
    name = "rubydev_fixture",
    base = "@benchmark_rubydev_amd64",
    layers = [":rubydev_fixture_layer"],
    tags = ["manual"],
)

image_outputs(
    name = "benchmark_rubydev",
    image = ":rubydev_fixture",
    image_name = "benchmarks/rubydev",
    visibility = ["//test/benchmarks/harness:__pkg__"],
)
