load("//tools:arch.bzl", "select_arch")
load("//tools:defs.bzl", "pkg_deb", "pkg_tar", "version")
load("//tools:release.bzl", "EXTRA_RELEASE_FILES", "RELEASE_FILES", "SIDECARS", "instrumented_release_tars")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

pkg_tar(
    name = "gvisor-bin-tar",
    files = SIDECARS,
    mode = "0755",
    package_dir = "gvisor-bin",
    tags = ["staging"],
)

pkg_tar(
    name = "debian-gvisor-bin",
    files = SIDECARS,
    mode = "0755",
    package_dir = "/usr/bin/gvisor-bin",
)

pkg_tar(
    name = "debian-bin",
    srcs = [
        "//runsc",
        "//shim:containerd-shim-runsc-v1",
    ],
    mode = "0755",
    package_dir = "/usr/bin",
)

pkg_tar(
    name = "debian-data",
    extension = "tar.gz",
    deps = [
        ":debian-bin",
        ":debian-gvisor-bin",
        "//shim:config",
    ],
)

genrule(
    name = "changelog",
    srcs = ["//tools/bazeldefs:version"],
    outs = ["changelog"],
    cmd = "VERSION=$$(cat $(location //tools/bazeldefs:version)); printf 'runsc (%s) unstable; urgency=medium\\n\\n  * See https://gvisor.dev/docs/ for release details.\\n\\n -- The gVisor Authors <gvisor-dev@googlegroups.com>  %s\\n' \"$$VERSION\" \"$$(date -R)\" > \"$@\"",
    stamp = 1,
)

pkg_deb(
    name = "debian",
    architecture = select_arch(
        amd64 = "amd64",
        arm64 = "arm64",
    ),
    changelog = ":changelog",
    conffiles = [
        "/etc/containerd/runsc.toml",
    ],
    data = ":debian-data",
    # Note that the description_file will be flatten (all newlines removed),
    # and therefore it is kept to a simple one-line description. The expected
    # format for debian packages is "short summary\nLonger explanation of
    # tool." and this is impossible with the flattening.
    description_file = "description",
    homepage = "https://gvisor.dev/",
    maintainer = "The gVisor Authors <gvisor-dev@googlegroups.com>",
    package = "runsc",
    package_file_name = "runsc.deb",
    postinst = "postinst.sh",
    version_file = version,
    visibility = [
        "//visibility:public",
    ],
)

# Downloadable release tarball containing all gVisor files.
# This is not the Debian package, but it is in this BUILD file anyway
# because it needs to contain the same set of files in practice, so
# easier to keep in sync.
pkg_tar(
    name = "gvisor-release-tar-bz2",
    extension = "tar.bz2",
    files = RELEASE_FILES,
    mode = "0755",
    package_file_name = "gvisor.tar.bz2",
    visibility = ["//visibility:public"],
)

# Same as :gvisor-release-tar-bz2 but zstd instead of bzip2
# Smaller and much, much faster to extract.
pkg_tar(
    name = "gvisor-release-tar-zstd",
    compressor = "//tools/zstd:compressor",
    extension = "tar.zstd",
    files = RELEASE_FILES,
    mode = "0755",
    package_file_name = "gvisor.tar.zstd",
    visibility = ["//visibility:public"],
)

# Release tarballs of instrumented gVisor builds, for fuzzing and testing.
# Same layout as the regular release tarballs, see instrumented_release_tars.

# Go coverage instrumentation (kcov), e.g. for syzkaller.
instrumented_release_tars(
    name = "gvisor-release-coverage",
    files = EXTRA_RELEASE_FILES,
    flavor = "coverage",
)

# Go race detector.
instrumented_release_tars(
    name = "gvisor-release-race",
    files = EXTRA_RELEASE_FILES,
    flavor = "race",
)

# Go coverage instrumentation (kcov) and race detector.
instrumented_release_tars(
    name = "gvisor-release-race-coverage",
    files = EXTRA_RELEASE_FILES,
    flavor = "race-coverage",
)
