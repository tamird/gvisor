load("@rules_distroless//distroless:defs.bzl", "cacerts")
load("@rules_foreign_cc//foreign_cc:defs.bzl", "configure_make", "make")
load("@rules_img//img:image.bzl", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("//tools:defs.bzl", "pkg_tar")
load(":busybox.bzl", "busybox_host_compiler", "busybox_nm")

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
    visibility = ["//images:__subpackages__"],
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

busybox_host_compiler(name = "busybox_hostcc")

busybox_nm(name = "busybox_nm")

make(
    name = "busybox_build",
    args = ["SKIP_STRIP=y"] + select({
        "@platforms//cpu:aarch64": ["ARCH=aarch64"],
        "@platforms//cpu:x86_64": ["ARCH=x86_64"],
    }),
    build_data = [
        ":busybox_hostcc",
        "busybox/make.sh",
        "@bzip2//:bzip2",
    ],
    data = [
        "busybox/pidfd.config",
        ":busybox_nm",
    ],
    env = {
        "BUSYBOX_BZIP2": "$(execpath @bzip2//:bzip2)",
        "BUSYBOX_CONFIG": "$(execpath busybox/pidfd.config)",
        "BUSYBOX_HOSTCC": "$(execpath :busybox_hostcc)",
        "BUSYBOX_NM": "$(execpath :busybox_nm)",
        # Keep generated configuration and banners independent of wall time.
        "KCONFIG_NOTIMESTAMP": "1",
    },
    lib_source = "@busybox_source//:source",
    out_binaries = ["busybox"],
    out_data_files = ["busybox.config"],
    postfix_script = """mkdir -p "$$INSTALLDIR/bin"
cp busybox "$$INSTALLDIR/bin/busybox"
cp .config "$$INSTALLDIR/busybox.config"
""",
    set_file_prefix_map = True,
    target_compatible_with = ["@platforms//os:linux"],
    targets = [
        "allnoconfig",
        "defconfig",
        "busybox",
    ],
    tool_prefix = "$(execpath busybox/make.sh)",
)

filegroup(
    name = "busybox_binary",
    srcs = [":busybox_build"],
    output_group = "busybox",
)

filegroup(
    name = "busybox_config",
    srcs = [":busybox_build"],
    output_group = "busybox.config",
)

image_layer(
    name = "musl_notice_layer",
    srcs = {"/usr/share/licenses/musl/NOTICE": "musl/NOTICE"},
    default_metadata = file_metadata(mode = "0644"),
    visibility = ["//images:__subpackages__"],
)

# Consumers install only the applet paths used by their own entrypoint.
image_layer(
    name = "busybox_layer",
    srcs = {
        "/bin/busybox": ":busybox_binary",
        "/usr/share/licenses/busybox/LICENSE": "@busybox_source//:LICENSE",
        "/usr/share/licenses/busybox/NOTICE": "busybox/NOTICE",
    },
    default_metadata = file_metadata(mode = "0644"),
    file_metadata = {"/bin/busybox": file_metadata(mode = "0755")},
    include_runfiles = False,
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
        "/usr/share/licenses/coreutils/NOTICE": "coreutils/NOTICE",
    },
    default_metadata = file_metadata(mode = "0644"),
    file_metadata = {"/usr/bin/timeout": file_metadata(mode = "0755")},
    include_runfiles = False,
    visibility = ["//images:__subpackages__"],
)
