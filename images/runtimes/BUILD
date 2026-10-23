load("@rules_img//img:image.bzl", "image_manifest")
load("//images:img.bzl", "image_outputs")
load("//tools:arch.bzl", "arch_genrule")

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

arch_genrule(
    name = "qualification_php_archives",
    src = ":php_archive",
    template = "qualification_php_archive_%s.tar",
)

arch_genrule(
    name = "qualification_php_contexts",
    src = ":php_context",
    template = "qualification_php_context_%s.tar",
)
