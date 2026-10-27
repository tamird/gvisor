load("//images:img.bzl", "image_outputs")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

image_outputs(
    name = "alpine",
    image = select({
        "@platforms//cpu:aarch64": "@benchmark_alpine_arm64",
        "@platforms//cpu:x86_64": "@benchmark_alpine_amd64",
    }),
    image_name = "benchmarks/alpine",
)
