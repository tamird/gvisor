load("@bazel_skylib//rules:write_file.bzl", "write_file")
load("@rules_cc//cc:defs.bzl", "cc_binary", "cc_library")
load("@rules_img//img:image.bzl", "image_index", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("//images:img.bzl", "image_outputs")
load("//tools:arch.bzl", "arch_genrule")
load("//tools:defs.bzl", "pkg_tar")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

# Select the image target ABI independently of the execution host.
_MUSL_PLATFORMS = select({
    "@platforms//cpu:aarch64": ["@llvm//platforms:linux_aarch64_musl"],
    "@platforms//cpu:x86_64": ["@llvm//platforms:linux_x86_64_musl"],
})

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

# Exercise containerd's resolver mounts through a symlink in the image.
image_layer(
    name = "symlink_resolv_layer",
    symlinks = {
        "/bin/cat": "/bin/busybox",
        "/bin/readlink": "/bin/busybox",
        "/bin/sleep": "/bin/busybox",
        "/etc/resolv.conf": "../tmp/resolv.conf",
    },
)

image_manifest(
    name = "symlink_resolv_manifest",
    env = {"PATH": "/usr/bin:/bin"},
    layers = [
        "//images:helper_filesystem",
        "//images:busybox_layer",
        "//images:musl_notice_layer",
        ":symlink_resolv_layer",
    ],
    tags = ["manual"],
    user = "0:0",
)

image_index(
    name = "symlink_resolv",
    manifests = [":symlink_resolv_manifest"],
    platforms = _MUSL_PLATFORMS,
    tags = ["manual"],
)

image_outputs(
    name = "symlink_resolv",
    image = ":symlink_resolv",
    image_name = "basic/symlink-resolv",
)

pkg_tar(
    name = "httpd_directories",
    empty_dirs = ["usr/local/apache2/logs"],
    mode = "0755",
)

image_layer(
    name = "httpd_fixture_layer",
    srcs = {
        "/usr/local/apache2/conf/httpd.conf": "httpd/httpd.conf",
        "/usr/local/apache2/htdocs/index.html": "@httpd_source//:docs/docroot/index.html",
        "/usr/local/bin/httpd-foreground": "httpd/httpd-foreground",
    },
    default_metadata = file_metadata(mode = "0644"),
    file_metadata = {"/usr/local/bin/httpd-foreground": file_metadata(mode = "0755")},
    symlinks = {
        "/bin/rm": "/bin/busybox",
        "/bin/sh": "/bin/busybox",
    },
)

image_manifest(
    name = "httpd_manifest",
    cmd = ["httpd-foreground"],
    config_fragment = "//images/httpd:ports.json",
    env = {
        "HTTPD_PREFIX": "/usr/local/apache2",
        "PATH": "/usr/local/apache2/bin:/usr/local/bin:/usr/bin:/bin",
    },
    layers = [
        "//images:helper_filesystem",
        "//images:busybox_layer",
        "//images:musl_notice_layer",
        "//images/httpd:accounts_layer",
        "//images/httpd:server_layer",
        ":httpd_directories",
        ":httpd_fixture_layer",
    ],
    stop_signal = "SIGWINCH",
    tags = ["manual"],
    user = "0:0",
    working_dir = "/usr/local/apache2",
)

image_index(
    name = "httpd",
    manifests = [":httpd_manifest"],
    platforms = _MUSL_PLATFORMS,
    tags = ["manual"],
)

image_outputs(
    name = "httpd",
    image = ":httpd",
    image_name = "basic/httpd",
)

# The integration test checks that mounting /tmp preserves image contents.
image_layer(
    name = "tmpfile_layer",
    srcs = {"/tmp/foo/file.txt": "tmpfile.txt"},
    create_parent_directories = "enabled",
    default_metadata = file_metadata(mode = "0644"),
    # The fixture must retain /tmp's sticky bit when adding its child directory.
    file_metadata = {"/tmp": file_metadata(mode = "1777")},
    symlinks = {"/bin/cat": "/bin/busybox"},
)

image_manifest(
    name = "tmpfile_manifest",
    env = {"PATH": "/usr/bin:/bin"},
    layers = [
        "//images:busybox_layer",
        "//images:musl_notice_layer",
        ":tmpfile_layer",
    ],
    tags = ["manual"],
    user = "0:0",
)

image_index(
    name = "tmpfile",
    manifests = [":tmpfile_manifest"],
    platforms = _MUSL_PLATFORMS,
    tags = ["manual"],
)

image_outputs(
    name = "tmpfile",
    image = ":tmpfile",
    image_name = "basic/tmpfile",
)

# Match the pidfd collection's TEST_GEN_PROGS order in Linux's Makefile.
_PIDFD_TESTS = [
    "pidfd_test",
    "pidfd_fdinfo_test",
    "pidfd_open_test",
    "pidfd_poll_test",
    "pidfd_wait",
    "pidfd_getfd_test",
    "pidfd_setns_test",
]

_SELFTESTS = "@linux_pidfd//:tools/testing/selftests/"

cc_library(
    name = "pidfd_headers",
    hdrs = [
        _SELFTESTS + "clone3/clone3_selftests.h",
        _SELFTESTS + "kselftest.h",
        _SELFTESTS + "kselftest_harness.h",
        _SELFTESTS + "pidfd/pidfd.h",
    ],
)

[
    cc_binary(
        name = name,
        srcs = [_SELFTESTS + "pidfd/" + name + ".c"],
        copts = [
            "-Wall",
            "-g",
            "-pthread",
        ],
        # These syscall tests need no runtime libraries beyond libc. Static
        # linkage lets them run with the image's small shell/utility base.
        linkopts = [
            "-static",
            "-pthread",
        ],
        # This is a target ABI constraint, not a host-platform restriction.
        target_compatible_with = ["@platforms//os:linux"],
        deps = [":pidfd_headers"],
    )
    for name in _PIDFD_TESTS
]

write_file(
    name = "pidfd_test_list",
    out = "pidfd-kselftest-list.txt",
    content = ["pidfd:" + name for name in _PIDFD_TESTS],
)

_PIDFD_FILES = {
    "/opt/kselftest/kselftest-list.txt": ":pidfd_test_list",
    "/opt/kselftest/kselftest/prefix.pl": _SELFTESTS + "kselftest/prefix.pl",
    "/opt/kselftest/kselftest/runner.sh": _SELFTESTS + "kselftest/runner.sh",
    "/opt/kselftest/run_kselftest.sh": _SELFTESTS + "run_kselftest.sh",
    "/usr/share/licenses/pidfd-tests/COPYING": "@linux_pidfd//:COPYING",
    "/usr/share/licenses/pidfd-tests/GPL-2.0": "@linux_pidfd//:LICENSES/preferred/GPL-2.0",
    "/usr/share/licenses/pidfd-tests/Linux-syscall-note": "@linux_pidfd//:LICENSES/exceptions/Linux-syscall-note",
    "/usr/share/licenses/pidfd-tests/license-rules.rst": "@linux_pidfd//:Documentation/process/license-rules.rst",
}

_PIDFD_FILES.update({
    "/opt/kselftest/pidfd/" + name: ":" + name
    for name in _PIDFD_TESTS
})

# Keep the exact patched sources available when diagnosing a selftest failure.
_PIDFD_FILES.update({
    "/src/pidfd-tests/pidfd/" + name: _SELFTESTS + "pidfd/" + name
    for name in [name + ".c" for name in _PIDFD_TESTS] + [
        "Makefile",
        "config",
        "pidfd.h",
    ]
})

_PIDFD_MODES = {
    "/opt/kselftest/pidfd/" + name: file_metadata(mode = "0755")
    for name in _PIDFD_TESTS
}

_PIDFD_MODES.update({
    "/opt/kselftest/" + name: file_metadata(mode = "0755")
    for name in [
        "run_kselftest.sh",
        "kselftest/runner.sh",
        "kselftest/prefix.pl",
    ]
})

image_layer(
    name = "pidfd_tests_layer",
    srcs = _PIDFD_FILES,
    default_metadata = file_metadata(mode = "0644"),
    file_metadata = _PIDFD_MODES,
    include_runfiles = False,
)

# kselftest uses GNU timeout's --foreground option to enforce each test's
# deadline. BusyBox supplies the other shell commands used by this collection.
image_layer(
    name = "pidfd_applets_layer",
    symlinks = {
        "/bin/" + applet: "/bin/busybox"
        for applet in [
            "basename",
            "cat",
            "cut",
            "dirname",
            "grep",
            "realpath",
            "sed",
            "sh",
            "sleep",
            "sort",
            "tee",
            "tr",
            "uniq",
            "wc",
        ]
    },
)

image_manifest(
    name = "pidfd_tests_manifest",
    cmd = [
        "/bin/sh",
        "-ec",
        """set -o pipefail
./run_kselftest.sh -c pidfd | tee /tmp/test.log
if grep -q 'not ok' /tmp/test.log; then
    echo 'FAILURE DETECTED'
    exit 1
else
    test "$?" -eq 1
fi
""",
    ],
    env = {"PATH": "/usr/bin:/bin"},
    layers = [
        "//images:helper_filesystem",
        "//images:busybox_layer",
        "//images:musl_notice_layer",
        ":pidfd_applets_layer",
        "//images:timeout_layer",
        ":pidfd_tests_layer",
    ],
    tags = ["manual"],
    user = "0:0",
    working_dir = "/opt/kselftest",
)

image_index(
    name = "pidfd_tests",
    manifests = [":pidfd_tests_manifest"],
    platforms = _MUSL_PLATFORMS,
    tags = ["manual"],
)

image_outputs(
    name = "pidfd_tests",
    image = ":pidfd_tests",
    image_name = "basic/pidfd-tests",
)

arch_genrule(
    name = "qualification_httpd_archives",
    src = ":httpd_archive",
    template = "qualification_httpd_archive_%s.tar",
)

arch_genrule(
    name = "qualification_httpd_contexts",
    src = ":httpd_context",
    template = "qualification_httpd_context_%s.tar",
)
