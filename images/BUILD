load("@rules_distroless//distroless:defs.bzl", "cacerts")
load("@rules_foreign_cc//foreign_cc:defs.bzl", "configure_make")
load("@rules_img//img:image.bzl", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
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

# Both static Go helpers use this base. Additional runtime commands belong to
# the image whose caller needs them.
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

# The PIDFD runner uses GNU timeout's exit-status and process-group behavior.
# Link it statically so scratch images do not need a distribution's loader.
configure_make(
    name = "timeout_build",
    args = [
        "-f",
        "Makefile",
        "-f",
        '"$$EXT_BUILD_ROOT$$/$(execpath coreutils/timeout.mk)"',
    ],
    # timeout needs none of these optional system libraries or message catalogs.
    configure_options = [
        "--disable-acl",
        "--disable-libcap",
        "--disable-libsmack",
        "--disable-nls",
        "--disable-xattr",
        "--without-libgmp",
        "--without-openssl",
        "--without-selinux",
    ] + select({
        "@platforms//cpu:aarch64": ["--host=aarch64-linux-musl"],
        "@platforms//cpu:x86_64": ["--host=x86_64-linux-musl"],
    }),
    data = ["coreutils/timeout.mk"],
    env = {
        # RBE workers run as root; configure's privilege-sensitive probes
        # otherwise refuse to proceed. This does not affect runtime privileges.
        "FORCE_UNSAFE_CONFIGURE": "1",
    },
    lib_source = "@coreutils_source//:source",
    linkopts = ["-static"],
    out_binaries = ["timeout"],
    out_include_dir = "",
    # configure compiles absolute source paths; keep worker roots out of output.
    set_file_prefix_map = True,
    target_compatible_with = [
        "@llvm//constraints/libc:musl",
        "@platforms//os:linux",
    ],
    targets = ["bazel-timeout"],
)

filegroup(
    name = "timeout_binary",
    srcs = [":timeout_build"],
    output_group = "timeout",
)

image_layer(
    name = "timeout_layer",
    srcs = {
        "/usr/bin/timeout": ":timeout_binary",
        "/usr/share/licenses/coreutils/COPYING": "@coreutils_source//:COPYING",
    },
    default_metadata = file_metadata(mode = "0644"),
    file_metadata = {"/usr/bin/timeout": file_metadata(mode = "0755")},
    include_runfiles = False,
    visibility = ["//images:__subpackages__"],
)
