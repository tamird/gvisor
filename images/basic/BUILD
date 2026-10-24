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

image_manifest(
    name = "redis",
    base = "@test_redis",
    tags = ["manual"],
)

image_outputs(
    name = "redis",
    image = ":redis",
    image_name = "basic/redis",
)

# Load the original config, including Docker's health check, without an OCI
# manifest conversion.
alias(
    name = "mysql",
    actual = select({
        "@platforms//cpu:aarch64": "@test_mysql_arm64",
        "@platforms//cpu:x86_64": "@test_mysql_amd64",
    }),
    tags = ["manual"],
)

image_outputs(
    name = "mysql",
    image = ":mysql",
    image_name = "basic/mysql",
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

arch_genrule(
    name = "qualification_redis_archives",
    src = ":redis_archive",
    template = "qualification_redis_archive_%s.tar",
)

arch_genrule(
    name = "qualification_redis_contexts",
    src = ":redis_context",
    template = "qualification_redis_context_%s.tar",
)

arch_genrule(
    name = "qualification_mysql_archives",
    src = ":mysql_archive",
    template = "qualification_mysql_archive_%s.tar",
)

arch_genrule(
    name = "qualification_mysql_contexts",
    src = ":mysql_context",
    template = "qualification_mysql_context_%s.tar",
)
