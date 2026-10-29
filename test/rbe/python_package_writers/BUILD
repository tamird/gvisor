load("@rules_pkg//pkg:pkg.bzl", "pkg_deb", "pkg_tar")

package(default_visibility = ["//visibility:public"])

# The scratch platform is inherited unchanged. Select the original Ubuntu
# execution image on each architecture for the ordinary packaging control.
[platform(
    name = "ubuntu_" + arch,
    exec_properties = {
        "container-image": "docker://mirror.gcr.io/library/ubuntu@sha256:b8b6ee6aa931ecd9d0d952abc34dc0e5f7c6a30c6bb71b079fe399fde0329c02",
    },
    parents = ["//tools/bazeldefs:rbe_linux_" + arch],
) for arch in ["amd64", "arm64"]]

pkg_tar(
    name = "payload",
    srcs = ["notice.txt"],
    package_dir = "share",
)

# Supply a tiny checked-in tar so MakeDeb can actually execute even if the
# independent PackageTar action fails on a different worker.
pkg_deb(
    name = "package",
    architecture = "all",
    data = "seed.tar.gz",
    description = "Python package writer qualification",
    maintainer = "The gVisor Authors",
    package = "gvisor-writer-input",
    version = "1",
)

# Both packaging tools have stamp=auto. Their target configurations are the
# stamped positive control; pkg_tar/pkg_deb above use the same tools in exec.
filegroup(
    name = "target_writers",
    srcs = [
        "@rules_pkg//pkg/private/deb:make_deb",
        "@rules_pkg//pkg/private/tar:build_tar",
    ],
)
