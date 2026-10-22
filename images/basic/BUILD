load("@rules_img//img:image.bzl", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("//images:img.bzl", "image_outputs")
load("//tools:arch.bzl", "arch_genrule")

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

# The ACL commands resolve the test's named user and group through these files.
image_layer(
    name = "libacl_accounts",
    srcs = {
        "/etc/group": "libacl/group",
        "/etc/passwd": "libacl/passwd",
    },
    default_metadata = file_metadata(mode = "0644"),
)

image_manifest(
    name = "libacl",
    base = "//images:helper_base",
    layers = [
        "@acl_image_packages//:flat",
        ":libacl_accounts",
    ],
    tags = ["manual"],
)

image_outputs(
    name = "libacl",
    image = ":libacl",
    image_name = "basic/libacl",
)

arch_genrule(
    name = "libacl_archives",
    src = ":libacl_archive",
    template = "libacl_%s.tar",
)

# The integration test checks that mounting /tmp preserves image contents.
image_layer(
    name = "tmpfile_layer",
    srcs = {"/tmp/foo/file.txt": "tmpfile/file.txt"},
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
