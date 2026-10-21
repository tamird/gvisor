load("@rules_distroless//distroless:defs.bzl", "cacerts")
load("@rules_img//img:image.bzl", "image_manifest")
load("//tools:defs.bzl", "pkg_tar")

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

# Shared filesystem and certificate data for native images. Additional runtime
# commands belong to the image whose caller needs them.
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
