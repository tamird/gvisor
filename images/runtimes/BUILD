load("@rules_img//img:image.bzl", "image_manifest")
load("//images:img.bzl", "image_outputs")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

image_manifest(
    name = "php",
    base = "@runtime_php",
    layers = ["@php_test_source//:fixtures"],
    tags = ["manual"],
    working_dir = "/root/php-8.5.11",
)

image_outputs(
    name = "php",
    image = ":php",
    image_name = "runtimes/php8.5.11",
)
