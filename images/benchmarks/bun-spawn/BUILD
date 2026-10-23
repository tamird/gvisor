load("@rules_img//img:image.bzl", "image_manifest")
load("@rules_img//img:layer.bzl", "image_layer")
load("//images:img.bzl", "image_outputs")
load("//tools:arch.bzl", "arch_genrule")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

image_layer(
    name = "script_layer",
    srcs = {"/spawn.js": "spawn.js"},
)

image_manifest(
    name = "bun_spawn",
    base = "@test_bun",
    layers = [":script_layer"],
    tags = ["manual"],
)

image_outputs(
    name = "bun_spawn",
    image = ":bun_spawn",
    image_name = "benchmarks/bun-spawn",
)

arch_genrule(
    name = "qualification_bun_archives",
    src = ":bun_spawn_archive",
    template = "qualification_bun_archive_%s.tar",
)

arch_genrule(
    name = "qualification_bun_contexts",
    src = ":bun_spawn_context",
    template = "qualification_bun_context_%s.tar",
)
