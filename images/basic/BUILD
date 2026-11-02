load("@rules_img//img:image.bzl", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("//images:img.bzl", "image_outputs")
load("//tools:defs.bzl", "cc_binary", "pkg_tar")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

filegroup(
    name = "sources",
    srcs = glob(["**"]),
    visibility = ["//images:__pkg__"],
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

_INTEGRATION_HELPERS = [
    "host_connect",
    "host_fd",
    "link_test",
    "tcp_loopback",
    "tcp_server",
    "tcp_stress_server",
    "test_copy_up",
    "test_rewinddir",
    "test_sticky",
]

# Install static Linux executables for the requested image architecture.
[
    cc_binary(
        name = name,
        srcs = ["integrationtest/%s.c" % name],
        copts = ["-O2"] + (["-pthread"] if name == "tcp_stress_server" else []),
        static = True,
        target_compatible_with = ["@platforms//os:linux"],
    )
    for name in _INTEGRATION_HELPERS
]

# /tmp must be writable by the nonroot host-socket test user as well.
pkg_tar(
    name = "integrationtest_filesystem",
    empty_dirs = ["tmp"],
    mode = "01777",
)

# These helpers are invoked from /root by the existing integration tests.
# Keep the socket client in the nonroot home as well for host-UID/GID checks.
image_layer(
    name = "integrationtest_programs",
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
    include_runfiles = False,
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
    symlinks = {
        # Package extraction does not run update-alternatives. Provide legacy
        # xtables entry points for runsc do's network setup.
        "/usr/sbin/ip6tables": "ip6tables-legacy",
        "/usr/sbin/ip6tables-restore": "ip6tables-legacy-restore",
        "/usr/sbin/ip6tables-save": "ip6tables-legacy-save",
        "/usr/sbin/iptables": "iptables-legacy",
        "/usr/sbin/iptables-restore": "iptables-legacy-restore",
        "/usr/sbin/iptables-save": "iptables-legacy-save",
        # base-files postinst normally migrates this path to /run. ip netns
        # uses /run/netns, while runsc do opens the same namespace via /var/run.
        "/var/run": "/run",
    },
)

image_manifest(
    name = "integrationtest_image",
    cmd = ["/bin/bash"],
    env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
    layers = [
        "@integration_test_packages//:flat",
        ":integrationtest_filesystem",
        ":integrationtest_programs",
    ],
    tags = ["manual"],
    target_compatible_with = ["@platforms//os:linux"],
    user = "0:0",
    working_dir = "/root",
)

image_outputs(
    name = "integrationtest",
    image = ":integrationtest_image",
    image_name = "basic/integrationtest",
)
