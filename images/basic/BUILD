load("@rules_img//img:image.bzl", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("//images:img.bzl", "image_outputs")
load("//tools:arch.bzl", "arch_genrule")
load("//tools:defs.bzl", "pkg_tar")

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

# Keep the package's policy defaults while applying the mode set by postinst.
genrule(
    name = "sudoers_file",
    srcs = ["@sudo_image_packages//sudo:data"],
    outs = ["sudoers"],
    cmd = "$(execpath @bsd_tar_toolchains//:resolved_target_toolchain) -xOf $(location @sudo_image_packages//sudo:data) ./etc/sudoers > $@",
    tools = ["@bsd_tar_toolchains//:resolved_target_toolchain"],
)

# Package extraction preserves sudo's setuid mode but does not run useradd or
# pam-auth-update. Declare the test accounts and the Unix PAM profile explicitly.
image_layer(
    name = "sudo_configuration",
    srcs = {
        "/etc/group": "sudo.group",
        "/etc/pam.d/common-account": "sudo.common-account",
        "/etc/pam.d/common-auth": "sudo.common-auth",
        "/etc/pam.d/common-password": "sudo.common-password",
        "/etc/pam.d/common-session": "sudo.common-session",
        "/etc/pam.d/common-session-noninteractive": "sudo.common-session",
        "/etc/passwd": "sudo.passwd",
        "/etc/shadow": "sudo.shadow",
        "/etc/sudoers": ":sudoers_file",
        "/etc/sudoers.d/test-admins": "sudo.sudoers",
    },
    default_metadata = file_metadata(mode = "0644"),
    file_metadata = {
        "/etc/shadow": file_metadata(mode = "0640", gid = 42),
        # sudo's postinst sets this mode when installing the package normally.
        "/etc/sudoers": file_metadata(mode = "0440"),
        "/etc/sudoers.d/test-admins": file_metadata(mode = "0440"),
    },
    # The privilege tests use this absolute path; the package installs usr/bin.
    symlinks = {"/bin/whoami": "/usr/bin/whoami"},
)

pkg_tar(
    name = "sudo_home_directories",
    empty_dirs = [
        "home",
        "home/alice",
        "home/bob",
    ],
    mode = "0750",
    modes = {"home": "0755"},
    owners = {
        "home/alice": "1000.1000",
        "home/bob": "1001.1001",
    },
)

image_manifest(
    name = "sudo",
    base = "//images:helper_base",
    layers = [
        "@sudo_image_packages//:flat",
        ":sudo_configuration",
        ":sudo_home_directories",
    ],
    tags = ["manual"],
    user = "alice",
)

image_outputs(
    name = "sudo",
    image = ":sudo",
    image_name = "basic/sudo",
)

arch_genrule(
    name = "sudo_archives",
    src = ":sudo_archive",
    template = "sudo_%s.tar",
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
