load("@rules_img//img:image.bzl", "image_manifest")
load("//images:img.bzl", "image_outputs")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

image_manifest(
    name = "redis",
    base = "@test_redis",
    tags = ["manual"],
)

image_outputs(
    name = "redis",
    image = ":redis",
    image_name = "benchmarks/redis",
)
