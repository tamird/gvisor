load("@rules_img//img:image.bzl", "image_index", "image_manifest")
load("@rules_img//img:layer.bzl", "layer_from_tar")
load("@rules_pkg//pkg:tar.bzl", "pkg_tar")
load("//tools/bazeldefs:go.bzl", "go_binary")
load(":observe.bzl", "observe_worker")

package(default_applicable_licenses = ["//:license"])

pkg_tar(
    name = "root_layer",
    empty_dirs = ["gvisor-worker-root"],
    mode = "0555",
)

layer_from_tar(
    name = "layer",
    src = ":root_layer",
    compress = "gzip",
    estargz = "disabled",
)

image_manifest(
    name = "image",
    layers = [":layer"],
)

image_index(
    name = "root",
    manifests = [":image"],
    platforms = [
        "//tools/bazeldefs:linux_amd64",
        "//tools/bazeldefs:linux_arm64",
    ],
)

filegroup(
    name = "layout",
    srcs = [":root"],
    output_group = "oci_layout",
)

go_binary(
    name = "observer",
    srcs = ["observer.go"],
    pure = True,
    static = True,
)

observe_worker(
    name = "observe",
    observer = ":observer",
)
