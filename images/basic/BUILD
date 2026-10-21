load("@rules_img//img:image.bzl", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("//images:img.bzl", "image_outputs")
load("//tools:arch.bzl", "arch_genrule")
load("//tools:defs.bzl", "cc_binary")

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

cc_binary(
    name = "host_connect",
    srcs = ["integrationtest/host_connect.c"],
    copts = ["-O2"],
    static = True,
    target_compatible_with = ["@platforms//os:linux"],
)

cc_binary(
    name = "host_fd",
    srcs = ["integrationtest/host_fd.c"],
    copts = ["-O2"],
    static = True,
    target_compatible_with = ["@platforms//os:linux"],
)

cc_binary(
    name = "link_test",
    srcs = ["integrationtest/link_test.c"],
    copts = ["-O2"],
    static = True,
    target_compatible_with = ["@platforms//os:linux"],
)

cc_binary(
    name = "tcp_loopback",
    srcs = ["integrationtest/tcp_loopback.c"],
    copts = ["-O2"],
    static = True,
    target_compatible_with = ["@platforms//os:linux"],
)

cc_binary(
    name = "tcp_server",
    srcs = ["integrationtest/tcp_server.c"],
    copts = ["-O2"],
    static = True,
    target_compatible_with = ["@platforms//os:linux"],
)

cc_binary(
    name = "tcp_stress_server",
    srcs = ["integrationtest/tcp_stress_server.c"],
    copts = [
        "-O2",
        "-pthread",
    ],
    static = True,
    target_compatible_with = ["@platforms//os:linux"],
)

cc_binary(
    name = "test_copy_up",
    srcs = ["integrationtest/test_copy_up.c"],
    copts = ["-O2"],
    static = True,
    target_compatible_with = ["@platforms//os:linux"],
)

cc_binary(
    name = "test_rewinddir",
    srcs = ["integrationtest/test_rewinddir.c"],
    copts = ["-O2"],
    static = True,
    target_compatible_with = ["@platforms//os:linux"],
)

cc_binary(
    name = "test_sticky",
    srcs = ["integrationtest/test_sticky.c"],
    copts = ["-O2"],
    static = True,
    target_compatible_with = ["@platforms//os:linux"],
)

# These helpers are invoked from /root by the existing integration tests.
# Keep the socket client in the nonroot home as well for host-UID/GID checks.
image_layer(
    name = "integration_test_programs",
    srcs = {
        "/etc/group": "integrationtest/group",
        "/etc/passwd": "integrationtest/passwd",
        "/home/nonroot/host_connect": ":host_connect",
        "/root/copy_up_testfile.txt": "integrationtest/copy_up_testfile.txt",
        "/root/host_connect": ":host_connect",
        "/root/host_fd": ":host_fd",
        "/root/link_test": ":link_test",
        "/root/ping4.sh": "integrationtest/ping4.sh",
        "/root/ping6.sh": "integrationtest/ping6.sh",
        "/root/tcp_loopback": ":tcp_loopback",
        "/root/tcp_server": ":tcp_server",
        "/root/tcp_stress_server": ":tcp_stress_server",
        "/root/test_copy_up": ":test_copy_up",
        "/root/test_rewinddir": ":test_rewinddir",
        "/root/test_sticky": ":test_sticky",
    },
    create_parent_directories = "enabled",
    default_metadata = file_metadata(mode = "0755"),
    file_metadata = {
        "/etc/group": file_metadata(mode = "0644"),
        "/etc/passwd": file_metadata(mode = "0644"),
        "/home/nonroot": file_metadata(
            gid = 1337,
            mode = "0755",
            uid = 1338,
        ),
        "/root": file_metadata(mode = "0700"),
        "/root/copy_up_testfile.txt": file_metadata(mode = "0644"),
    },
    # Package extraction does not run update-alternatives. Preserve the legacy
    # iptables interface used by the original Ubuntu Bionic image.
    symlinks = {
        "/usr/sbin/ip6tables": "ip6tables-legacy",
        "/usr/sbin/ip6tables-restore": "ip6tables-legacy-restore",
        "/usr/sbin/ip6tables-save": "ip6tables-legacy-save",
        "/usr/sbin/iptables": "iptables-legacy",
        "/usr/sbin/iptables-restore": "iptables-legacy-restore",
        "/usr/sbin/iptables-save": "iptables-legacy-save",
    },
)

image_manifest(
    name = "integration_test_image",
    base = "//images:helper_base",
    cmd = ["/bin/bash"],
    env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
    layers = [
        "@integration_test_packages//:flat",
        ":integration_test_programs",
    ],
    tags = ["manual"],
    target_compatible_with = ["@platforms//os:linux"],
    working_dir = "/root",
)

image_outputs(
    name = "integration_test",
    image = ":integration_test_image",
    image_name = "basic/integrationtest",
)

arch_genrule(
    name = "integration_test_archives",
    src = ":integration_test_archive",
    template = "integration_test_%s.tar",
)

# Qualification-only: compile each existing helper for both supported CPUs.
arch_genrule(
    name = "host_connect_qualification",
    src = ":host_connect",
    tags = ["qualification-output-group=default"],
    template = "host_connect_%s",
)

arch_genrule(
    name = "host_fd_qualification",
    src = ":host_fd",
    tags = ["qualification-output-group=default"],
    template = "host_fd_%s",
)

arch_genrule(
    name = "link_test_qualification",
    src = ":link_test",
    tags = ["qualification-output-group=default"],
    template = "link_test_%s",
)

arch_genrule(
    name = "tcp_loopback_qualification",
    src = ":tcp_loopback",
    tags = ["qualification-output-group=default"],
    template = "tcp_loopback_%s",
)

arch_genrule(
    name = "tcp_server_qualification",
    src = ":tcp_server",
    tags = ["qualification-output-group=default"],
    template = "tcp_server_%s",
)

arch_genrule(
    name = "tcp_stress_server_qualification",
    src = ":tcp_stress_server",
    tags = ["qualification-output-group=default"],
    template = "tcp_stress_server_%s",
)

arch_genrule(
    name = "test_copy_up_qualification",
    src = ":test_copy_up",
    tags = ["qualification-output-group=default"],
    template = "test_copy_up_%s",
)

arch_genrule(
    name = "test_rewinddir_qualification",
    src = ":test_rewinddir",
    tags = ["qualification-output-group=default"],
    template = "test_rewinddir_%s",
)

arch_genrule(
    name = "test_sticky_qualification",
    src = ":test_sticky",
    tags = ["qualification-output-group=default"],
    template = "test_sticky_%s",
)
