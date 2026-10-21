load("@rules_distroless//distroless:defs.bzl", "cacerts")
load("@rules_img//img:image.bzl", "image_manifest")
load("//tools:defs.bzl", "pkg_tar")
load(":package_layer.bzl", "package_layer")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

cacerts(
    name = "helper_ca_certificates",
    package = "@helper_image_packages//ca-certificates:data",
)

pkg_tar(
    name = "helper_filesystem",
    empty_dirs = ["tmp"],
    mode = "01777",
)

# Native helper images share this base. Additional runtime commands belong
# to the image whose caller needs them.
image_manifest(
    name = "helper_base",
    env = {
        "PATH": "/usr/bin:/bin",
        "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
    },
    layers = [
        ":helper_ca_certificates",
        ":helper_filesystem",
    ],
    tags = ["manual"],
    # Image metadata follows the target platform, independently of the host.
    target_compatible_with = ["@platforms//os:linux"],
    user = "0:0",
    visibility = ["//images:__subpackages__"],
)

# GNU commands share one C runtime, loader and license layer. Their executables
# and any additional libraries belong to the images that need them.
package_layer(
    name = "helper_coreutils_runtime",
    members = [
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
    ],
    packages = [
        # Common license texts referenced by the package notices.
        "@helper_image_packages//base-files:data",
        # Notice for the selected GNU commands.
        "@helper_image_packages//coreutils:data",
        # Shared C runtime and dynamic loader.
        "@helper_image_packages//libc6:data",
    ],
    visibility = ["//images:__subpackages__"],
)
