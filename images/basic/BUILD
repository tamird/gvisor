load("@bazel_skylib//rules:write_file.bzl", "write_file")
load("@rules_cc//cc:defs.bzl", "cc_binary", "cc_library")
load("@rules_img//img:image.bzl", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("//images:img.bzl", "image_outputs", "package_layer")
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
_PIDFD_TIMEOUT_PACKAGES = [
    # Common license texts referenced by the runtime packages' notices.
    "@helper_image_packages//base-files:data",
    # GNU timeout, including its signal and process-group handling.
    "@helper_image_packages//coreutils:data",
    # C runtime and dynamic loader for the packaged timeout executable.
    "@helper_image_packages//libc6:data",
]

_PIDFD_TIMEOUT_MEMBERS = [
    "./usr/bin/timeout",
    "./lib/{triplet}/libc.so.6",
    "./lib/{triplet}/{loader}",
    "./{interpreter}",
    "./usr/share/doc/coreutils/copyright",
    "./usr/share/doc/libc6/copyright",
    "./usr/share/doc/base-files/copyright",
    "./usr/share/common-licenses/GPL-2",
    "./usr/share/common-licenses/GPL-3",
    "./usr/share/common-licenses/LGPL-2.1",
    "./usr/share/common-licenses/GFDL-1.3",
]

package_layer(
    name = "pidfd_timeout_layer",
    out = "pidfd-timeout.tar",
    members = _PIDFD_TIMEOUT_MEMBERS,
    packages = _PIDFD_TIMEOUT_PACKAGES,
)

image_manifest(
    name = "pidfd_tests",
    base = "@test_busybox",
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
    layers = [
        ":pidfd_timeout_layer",
        ":pidfd_tests_layer",
    ],
    tags = ["manual"],
    working_dir = "/opt/kselftest",
)

image_outputs(
    name = "pidfd_tests",
    image = ":pidfd_tests",
    image_name = "basic/pidfd-tests",
)
