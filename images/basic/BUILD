load("@bazel_skylib//rules:write_file.bzl", "write_file")
load("@rules_cc//cc:defs.bzl", "cc_binary", "cc_library")
load("@rules_img//img:image.bzl", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("//images:img.bzl", "image_outputs")

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
    name = "pidfd_tests",
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

image_outputs(
    name = "pidfd_tests",
    image = ":pidfd_tests",
    image_name = "basic/pidfd-tests",
)
