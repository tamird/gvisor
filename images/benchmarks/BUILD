load("@bazel_skylib//rules:write_file.bzl", "write_file")
load("@rules_img//img:image.bzl", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("//images:img.bzl", "image_outputs")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

_HTTP_PAYLOAD_SIZES = [
    1,
    10,
    100,
    1024,
    10240,
]

[
    write_file(
        name = "http_payload_%d" % size,
        out = "latin%dk.txt" % size,
        content = ["\000" * (size * 1024)],
    )
    for size in _HTTP_PAYLOAD_SIZES
]

image_layer(
    name = "http_payload_layer",
    srcs = {
        "/local/latin%dk.txt" % size: ":http_payload_%d" % size
        for size in _HTTP_PAYLOAD_SIZES
    },
    default_metadata = file_metadata(mode = "0644"),
)


write_file(
    name = "nginx_index",
    out = "nginx-index.html",
    content = [],
)

image_layer(
    name = "nginx_config_layer",
    srcs = {
        "/etc/nginx/nginx.conf": "nginx/nginx.conf",
        "/etc/nginx/nginx_gofer.conf": "nginx/nginx_gofer.conf",
        "/local/index.html": ":nginx_index",
    },
    default_metadata = file_metadata(mode = "0644"),
)

image_manifest(
    name = "nginx",
    base = "@benchmark_nginx",
    layers = [
        ":http_payload_layer",
        ":nginx_config_layer",
    ],
    tags = ["manual"],
)

image_outputs(
    name = "nginx",
    archive_visibility = ["//test/benchmarks/harness:__pkg__"],
    image = ":nginx",
    image_name = "benchmarks/nginx",
)
