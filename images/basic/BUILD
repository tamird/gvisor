load("@rules_img//img:image.bzl", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("//images:img.bzl", "image_outputs")
load("//tools:defs.bzl", "go_binary")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

image_manifest(
    name = "alpine",
    base = "@test_alpine",
    tags = ["manual"],
)

image_outputs(
    name = "alpine",
    image = ":alpine",
    image_name = "basic/alpine",
)

image_manifest(
    name = "busybox",
    base = "@test_busybox",
    tags = ["manual"],
)

image_outputs(
    name = "busybox",
    image = ":busybox",
    image_name = "basic/busybox",
)

go_binary(
    name = "tcpdump_sender",
    srcs = ["tcpdump_sender.go"],
    pure = True,
    static = True,
)

image_layer(
    name = "tcpdump_layer",
    srcs = {
        "/usr/bin/tcpdump": "@tcpdump",
        "/usr/bin/tcpdump_sender": ":tcpdump_sender",
        "/usr/share/licenses/libpcap/LICENSE": "@libpcap//:LICENSE",
        "/usr/share/licenses/tcpdump/LICENSE": "@tcpdump//:LICENSE",
    },
    include_runfiles = False,
)

image_manifest(
    name = "tcpdump",
    base = "@test_busybox",
    layers = [":tcpdump_layer"],
    tags = ["manual"],
)

image_outputs(
    name = "tcpdump",
    image = ":tcpdump",
    image_name = "basic/tcpdump",
)

# The integration test checks that mounting /tmp preserves image contents.
image_layer(
    name = "tmpfile_layer",
    srcs = {"/tmp/foo/file.txt": "tmpfile.txt"},
    create_parent_directories = "enabled",
    default_metadata = file_metadata(mode = "0644"),
    # Keep the base image's sticky /tmp when adding its child directory.
    file_metadata = {"/tmp": file_metadata(mode = "1777")},
)

image_manifest(
    name = "tmpfile",
    base = "@test_alpine",
    layers = [":tmpfile_layer"],
    tags = ["manual"],
)

image_outputs(
    name = "tmpfile",
    image = ":tmpfile",
    image_name = "basic/tmpfile",
)
