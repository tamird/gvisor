load("@rules_img//img:image.bzl", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("//images:img.bzl", "image_outputs")
load("//tools:arch.bzl", "arch_genrule")
load("//tools:defs.bzl", "go_binary", "pkg_tar")

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

image_layer(
    name = "fsstress_layer",
    srcs = {
        "/run.sh": "fsstress/run.sh",
        "/usr/bin/fsstress": "@ltp//:fsstress",
        "/usr/share/doc/fsstress/COPYING": "@ltp//:COPYING",
    },
    create_parent_directories = "enabled",
    default_metadata = file_metadata(mode = "0644"),
    file_metadata = {
        "/run.sh": file_metadata(mode = "0755"),
        "/usr/bin/fsstress": file_metadata(mode = "0755"),
    },
    include_runfiles = False,
)

image_manifest(
    name = "fsstress",
    base = "@test_busybox",
    cmd = [],
    entrypoint = ["/run.sh"],
    layers = [":fsstress_layer"],
    tags = ["manual"],
    working_dir = "/",
)

image_outputs(
    name = "fsstress",
    image = ":fsstress",
    image_name = "basic/fsstress",
)

image_layer(
    name = "libacl_accounts",
    srcs = {
        "/etc/group": "libacl.group",
        "/etc/passwd": "libacl.passwd",
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
        "/usr/share/licenses/bison/NOTICE": "tcpdump/bison.NOTICE",
        "/usr/share/licenses/libpcap/NOTICE": "tcpdump/libpcap.NOTICE",
        "/usr/share/licenses/tcpdump/NOTICE": "tcpdump/tcpdump.NOTICE",
    },
    file_metadata = {
        "/usr/share/licenses/bison/NOTICE": file_metadata(mode = "0644"),
        "/usr/share/licenses/libpcap/NOTICE": file_metadata(mode = "0644"),
        "/usr/share/licenses/tcpdump/NOTICE": file_metadata(mode = "0644"),
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
        "/etc/shadow": file_metadata(
            gid = 42,
            mode = "0640",
        ),
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

# Qualification-only originals for controlled resource-label comparisons.
# Preserve original target labels in layer history as well as installed bytes.
image_layer(
    name = "tmpfile_layer_before",
    srcs = {"/tmp/foo/file.txt": "tmpfile.txt"},
    create_parent_directories = "enabled",
    default_metadata = file_metadata(mode = "0644"),
    # Keep the base image's sticky /tmp when adding its child directory.
    file_metadata = {"/tmp": file_metadata(mode = "1777")},
    history = ["bazel build //images/basic:tmpfile_layer"],
)

image_manifest(
    name = "tmpfile_before",
    base = "@test_alpine",
    layers = [":tmpfile_layer_before"],
    tags = ["manual"],
)

image_outputs(
    name = "tmpfile_before",
    image = ":tmpfile_before",
    image_name = "basic/tmpfile",
)

image_layer(
    name = "sudo_configuration_before",
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
        "/etc/shadow": file_metadata(
            gid = 42,
            mode = "0640",
        ),
        # sudo's postinst sets this mode when installing the package normally.
        "/etc/sudoers": file_metadata(mode = "0440"),
        "/etc/sudoers.d/test-admins": file_metadata(mode = "0440"),
    },
    history = ["bazel build //images/basic:sudo_configuration"],
    # The privilege tests use this absolute path; the package installs usr/bin.
    symlinks = {"/bin/whoami": "/usr/bin/whoami"},
)

image_manifest(
    name = "sudo_before",
    base = "//images:helper_base",
    layers = [
        "@sudo_image_packages//:flat",
        ":sudo_configuration_before",
        ":sudo_home_directories",
    ],
    tags = ["manual"],
    user = "alice",
)

image_outputs(
    name = "sudo_before",
    image = ":sudo_before",
    image_name = "basic/sudo",
)

image_layer(
    name = "libacl_accounts_before",
    srcs = {
        "/etc/group": "libacl.group",
        "/etc/passwd": "libacl.passwd",
    },
    default_metadata = file_metadata(mode = "0644"),
    history = ["bazel build //images/basic:libacl_accounts"],
)

image_manifest(
    name = "libacl_before",
    base = "//images:helper_base",
    layers = [
        "@acl_image_packages//:flat",
        ":libacl_accounts_before",
    ],
    tags = ["manual"],
)

image_outputs(
    name = "libacl_before",
    image = ":libacl_before",
    image_name = "basic/libacl",
)

image_layer(
    name = "tcpdump_layer_before",
    srcs = {
        "/usr/bin/tcpdump": "@tcpdump",
        "/usr/bin/tcpdump_sender": ":tcpdump_sender",
        "/usr/share/licenses/bison/NOTICE": "bison.NOTICE",
        "/usr/share/licenses/libpcap/NOTICE": "libpcap.NOTICE",
        "/usr/share/licenses/tcpdump/NOTICE": "tcpdump.NOTICE",
    },
    file_metadata = {
        "/usr/share/licenses/bison/NOTICE": file_metadata(mode = "0644"),
        "/usr/share/licenses/libpcap/NOTICE": file_metadata(mode = "0644"),
        "/usr/share/licenses/tcpdump/NOTICE": file_metadata(mode = "0644"),
    },
    history = ["bazel build //images/basic:tcpdump_layer"],
    include_runfiles = False,
)

image_manifest(
    name = "tcpdump_before",
    base = "@test_busybox",
    layers = [":tcpdump_layer_before"],
    tags = ["manual"],
)

image_outputs(
    name = "tcpdump_before",
    image = ":tcpdump_before",
    image_name = "basic/tcpdump",
)

image_layer(
    name = "fsstress_layer_before",
    srcs = {
        "/run.sh": "fsstress.sh",
        "/usr/bin/fsstress": "@ltp//:fsstress",
        "/usr/share/doc/fsstress/COPYING": "@ltp//:COPYING",
    },
    create_parent_directories = "enabled",
    default_metadata = file_metadata(mode = "0644"),
    file_metadata = {
        "/run.sh": file_metadata(mode = "0755"),
        "/usr/bin/fsstress": file_metadata(mode = "0755"),
    },
    history = ["bazel build //images/basic:fsstress_layer"],
    include_runfiles = False,
)

image_manifest(
    name = "fsstress_before",
    base = "@test_busybox",
    cmd = [],
    entrypoint = ["/run.sh"],
    layers = [":fsstress_layer_before"],
    tags = ["manual"],
    working_dir = "/",
)

image_outputs(
    name = "fsstress_before",
    image = ":fsstress_before",
    image_name = "basic/fsstress",
)
