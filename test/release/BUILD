load("@rules_oci//oci:defs.bzl", "oci_image", "oci_load")
load("//test/docker:defs.bzl", "docker_daemon_config")
load("//tools:arch.bzl", "arch_genrule")
load("//tools:defs.bzl", "pkg_tar")
load("//tools/bazeldefs:platforms.bzl", "docker_test_exec_properties")
load(":defs.bzl", "release_artifacts", "repository_test")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

# APT data archives do not execute maintainer scripts or update-alternatives.
# Declare the two command links used by the canonical release scripts and Perl.
pkg_tar(
    name = "tool_layout",
    empty_dirs = ["tmp"],
    modes = {"tmp": "01777"},
    symlinks = {
        "bin/sh": "/bin/bash",
        "usr/bin/awk": "/usr/bin/mawk",
    },
    tags = ["manual"],
)

oci_image(
    name = "tools_image",
    architecture = "amd64",
    entrypoint = ["/bin/bash"],
    env = {
        "LC_ALL": "C",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    },
    os = "linux",
    tags = ["manual"],
    tars = [
        "@release_tools//:flat",
        ":tool_layout",
    ],
    user = "0:0",
    workdir = "/workspace",
)

oci_load(
    name = "tools_archive",
    image = ":tools_image",
    repo_tags = ["gvisor.dev/images/release-tools:latest"],
    tags = ["manual"],
)

filegroup(
    name = "tools_tar",
    srcs = [":tools_archive"],
    output_group = "tarball",
    tags = ["manual"],
)

# Match the public release test's two architecture builds and three packages.
# Both target architectures are built using the coordinator's AMD64 tools.
arch_genrule(
    name = "debian_packages",
    src = "//debian",
    tags = ["manual"],
    template = "artifacts/%s/runsc.deb",
)

arch_genrule(
    name = "bzip2_archives",
    src = "//debian:gvisor-release-tar-bz2",
    tags = ["manual"],
    template = "artifacts/%s/gvisor.tar.bz2",
)

arch_genrule(
    name = "zstd_archives",
    src = "//debian:gvisor-release-tar-zstd",
    tags = ["manual"],
    template = "artifacts/%s/gvisor.tar.zstd",
)

release_artifacts(
    name = "artifacts",
    srcs = [
        ":bzip2_archives",
        ":debian_packages",
        ":zstd_archives",
    ],
    tags = ["manual"],
)

genrule(
    name = "artifacts_manifest",
    srcs = [":artifacts"],
    outs = ["artifacts.txt"],
    cmd = "printf '%s\\n' $(rootpaths :artifacts) > $@",
    tags = ["manual"],
)

docker_daemon_config(
    name = "docker_config",
    testonly = True,
    images = [":tools_tar"],
    tags = ["manual"],
)

repository_test(
    name = "repository_test",
    size = "large",
    command = "repository_test.sh",
    command_args = [
        "$(rootpath :artifacts_manifest)",
        "$(rootpath //tools:make_test_key.sh)",
        "$(rootpath //tools:make_release.sh)",
        "$(rootpath //tools:make_apt.sh)",
        "$(rootpath :repository_test_inner.sh)",
    ],
    data = [
        "repository_test_inner.sh",
        ":artifacts",
        ":artifacts_manifest",
        "//tools:make_apt.sh",
        "//tools:make_release.sh",
        "//tools:make_test_key.sh",
    ],
    docker_config = ":docker_config",
    exec_properties = docker_test_exec_properties(free_disk = "20GB"),
    tags = ["manual"],
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
)
