load("@rules_img//img:image.bzl", "image_manifest")
load("//images:img.bzl", "image_outputs")
load("//tools:arch.bzl", "arch_genrule")

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

arch_genrule(
    name = "qualification_redis_archives",
    src = ":redis_archive",
    template = "qualification_redis_archive_%s.tar",
)

arch_genrule(
    name = "qualification_redis_contexts",
    src = ":redis_context",
    template = "qualification_redis_context_%s.tar",
)
