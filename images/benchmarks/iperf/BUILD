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
    name = "iperf_layer",
    srcs = {
        "/usr/bin/iperf3": "@iperf//:iperf3",
        "/usr/share/doc/iperf3/LICENSE": "@iperf//:LICENSE",
        "/usr/share/doc/openssl/LICENSE.txt": "@openssl//:LICENSE.txt",
    },
    create_parent_directories = "enabled",
    default_metadata = file_metadata(mode = "0644"),
    file_metadata = {"/usr/bin/iperf3": file_metadata(mode = "0755")},
    include_runfiles = False,
)

image_manifest(
    name = "iperf_image",
    base = "@benchmark_ubuntu",
    cmd = ["/usr/bin/iperf3"],
    layers = [":iperf_layer"],
    tags = ["manual"],
    # Match the executable's Linux platform, independently of the build host.
    target_compatible_with = ["@platforms//os:linux"],
)

image_outputs(
    name = "iperf",
    image = ":iperf_image",
    image_name = "benchmarks/iperf",
)
