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

genrule(
    name = "filecap_cat",
    srcs = ["@filecap_image_packages//coreutils:data"],
    outs = ["filecap/cat"],
    cmd = "$(execpath @bsd_tar_toolchains//:resolved_target_toolchain) -xOf $(location @filecap_image_packages//coreutils:data) ./bin/cat > $@",
    tools = ["@bsd_tar_toolchains//:resolved_target_toolchain"],
)

# Encode CAP_NET_ADMIN in Linux's little-endian vfs_cap_data revision 2, with
# the effective flag set. Declaring the attribute in the archive avoids needing
# CAP_SETFCAP on the build worker; the runtime tests check the loaded attribute.
image_layer(
    name = "filecap_cat_layer",
    srcs = {"/mnt/cat": ":filecap_cat"},
    default_metadata = file_metadata(
        mode = "0755",
        pax_records = {"SCHILY.xattr.security.capability": "\001\000\000\002\000\020\000\000" + "\000" * 12},
    ),
)

image_manifest(
    name = "filecap",
    base = "//images:helper_base",
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
    layers = [
        "@filecap_image_packages//:flat",
        ":filecap_cat_layer",
    ],
    tags = ["manual"],
)

image_outputs(
    name = "filecap",
    image = ":filecap",
    image_name = "basic/filecap",
)

arch_genrule(
    name = "filecap_archives",
    src = ":filecap_archive",
    template = "filecap_%s.tar",
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
