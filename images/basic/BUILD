load("@bazel_skylib//rules:write_file.bzl", "write_file")
load("@rules_cc//cc:defs.bzl", "cc_binary", "cc_library")
load("@rules_img//img:image.bzl", "image_index", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("//images:img.bzl", "image_outputs")
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

pkg_tar(
    name = "busybox_home",
    empty_dirs = ["root"],
    mode = "0700",
)

# The containerd HOME tests resolve the root account from /etc/passwd.
# Leave HOME unset so the image cannot supply the value being tested.
image_layer(
    name = "busybox_filesystem",
    srcs = {
        "/etc/group": "busybox/group",
        "/etc/passwd": "busybox/passwd",
    },
    create_parent_directories = "enabled",
    default_metadata = file_metadata(mode = "0644"),
    symlinks = {
        "/bin/" + applet: "/bin/busybox"
        for applet in [
            "dd",
            "df",
            "grep",
            "setsid",
            "sh",
            "sleep",
            "stat",
            "true",
        ]
    },
)

image_manifest(
    name = "busybox_manifest",
    cmd = ["sh"],
    env = {"PATH": "/usr/bin:/bin"},
    layers = [
        "//images:helper_filesystem",
        "//images:busybox_layer",
        "//images:musl_notice_layer",
        ":busybox_home",
        ":busybox_filesystem",
    ],
    tags = ["manual"],
    user = "0:0",
)

image_index(
    name = "busybox",
    manifests = [":busybox_manifest"],
    platforms = _MUSL_PLATFORMS,
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
    symlinks = {"/bin/sh": "/bin/busybox"},
)

image_manifest(
    name = "fsstress_manifest",
    entrypoint = ["/run.sh"],
    env = {"PATH": "/usr/bin:/bin"},
    layers = [
        "//images:helper_filesystem",
        "//images:busybox_layer",
        "//images:musl_notice_layer",
        ":fsstress_layer",
    ],
    tags = ["manual"],
    user = "0:0",
    working_dir = "/",
)

image_index(
    name = "fsstress",
    manifests = [":fsstress_manifest"],
    platforms = _MUSL_PLATFORMS,
    tags = ["manual"],
)

image_outputs(
    name = "fsstress",
    image = ":fsstress",
    image_name = "basic/fsstress",
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
