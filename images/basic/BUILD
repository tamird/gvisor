load("@rules_img//img:image.bzl", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("@rules_pkg//pkg:mappings.bzl", "pkg_attributes", "pkg_files")
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

# The Docker context may be built on a different distribution or architecture.
# Install static Linux executables for the requested target.
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

pkg_files(
    name = "integrationtest_programs",
    srcs = _INTEGRATION_HELPERS + [
        "integrationtest/ping4.sh",
        "integrationtest/ping6.sh",
    ],
    attributes = pkg_attributes(mode = "0755"),
)

pkg_files(
    name = "integrationtest_files",
    srcs = [
        "integrationtest/Dockerfile.runtime",
        "integrationtest/copy_up_testfile.txt",
    ],
    renames = {"integrationtest/Dockerfile.runtime": "Dockerfile"},
)

pkg_tar(
    name = "integrationtest_context",
    srcs = [
        ":integrationtest_files",
        ":integrationtest_programs",
    ],
    package_dir = "images/basic/integrationtest",
)
