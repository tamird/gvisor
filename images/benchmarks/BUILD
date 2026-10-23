load("@rules_img//img:image.bzl", "image_index", "image_manifest")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("//images:img.bzl", "image_outputs", "package_layer")
load("//tools:arch.bzl", "arch_genrule")
load("//tools:defs.bzl", "cc_binary", "go_binary")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

# Select the image target ABI independently of the execution host.
_MUSL_PLATFORMS = select({
    "@platforms//cpu:aarch64": ["@llvm//platforms:linux_aarch64_musl"],
    "@platforms//cpu:x86_64": ["@llvm//platforms:linux_x86_64_musl"],
})

_HTTPD_SIZES_KIB = [1, 10, 100, 1024, 10240]

# Keep the existing benchmark's exact zero-filled payloads and filenames.
[
    genrule(
        name = "httpd_payload_%dk" % size,
        outs = ["httpd/latin%dk.txt" % size],
        cmd = '"$(execpath //images:busybox_binary)" dd if=/dev/zero of=$@ bs=1024 count=%d' % size,
        # tools selects the execution configuration, not the image CPU.
        exec_compatible_with = ["@platforms//os:linux"],
        tools = ["//images:busybox_binary"],
    )
    for size in _HTTPD_SIZES_KIB
]

filegroup(
    name = "httpd_payloads",
    srcs = [":httpd_payload_%dk" % size for size in _HTTPD_SIZES_KIB],
)

_HTTPD_FILES = {
    "/local/latin%dk.txt" % size: ":httpd_payload_%dk" % size
    for size in _HTTPD_SIZES_KIB
}
_HTTPD_FILES["/usr/local/apache2/conf/httpd.conf"] = "httpd/httpd.conf"

image_layer(
    name = "httpd_fixture_layer",
    srcs = _HTTPD_FILES,
    default_metadata = file_metadata(mode = "0644"),
    symlinks = {
        "/bin/cp": "/bin/busybox",
        "/bin/mkdir": "/bin/busybox",
        "/bin/sh": "/bin/busybox",
        "/usr/sbin/apache2": "/usr/local/apache2/bin/httpd",
    },
)

image_manifest(
    name = "httpd_manifest",
    env = {
        "APACHE_LOG_DIR": "/tmp",
        "APACHE_PID_FILE": "/tmp/apache.pid",
        "APACHE_RUN_DIR": "/tmp",
        "APACHE_RUN_GROUP": "nogroup",
        "APACHE_RUN_USER": "nobody",
        "PATH": "/usr/sbin:/usr/bin:/bin",
    },
    layers = [
        "//images:helper_filesystem",
        "//images:busybox_layer",
        "//images:musl_notice_layer",
        "//images/httpd:accounts_layer",
        "//images/httpd:server_layer",
        ":httpd_fixture_layer",
    ],
    tags = ["manual"],
    user = "0:0",
)

image_index(
    name = "httpd",
    manifests = [":httpd_manifest"],
    platforms = _MUSL_PLATFORMS,
    tags = ["manual"],
)

image_outputs(
    name = "httpd",
    image = ":httpd",
    image_name = "benchmarks/httpd",
)

cc_binary(
    name = "syscallbench",
    srcs = ["syscallbench.c"],
    copts = ["-O2"],
    linkopts = ["-static"],
    # The assembly loop and seccomp filters use the Linux x86-64 ABI.
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
)

image_layer(
    name = "syscallbench_layer",
    srcs = {"/usr/bin/syscallbench": ":syscallbench"},
    default_metadata = file_metadata(mode = "0755"),
    # The benchmark keeps the container alive while executing each mode.
    symlinks = {"/bin/sleep": "/bin/busybox"},
)

image_manifest(
    name = "syscallbench_manifest",
    env = {"PATH": "/usr/bin:/bin"},
    layers = [
        "//images:busybox_layer",
        "//images:musl_notice_layer",
        ":syscallbench_layer",
    ],
    tags = ["manual"],
    user = "0:0",
)

# The existing benchmark assembly supports only the Linux x86-64 target.
image_index(
    name = "syscallbench_image",
    manifests = [":syscallbench_manifest"],
    platforms = ["@llvm//platforms:linux_x86_64_musl"],
    tags = ["manual"],
    target_compatible_with = ["@platforms//cpu:x86_64"],
)

image_outputs(
    name = "syscallbench",
    image = ":syscallbench_image",
    image_name = "benchmarks/syscallbench",
)

go_binary(
    name = "profilehelper",
    srcs = ["profile-helper/main.go"],
    pure = True,
    static = True,
)

image_layer(
    name = "profile_helper_layer",
    srcs = {"/usr/bin/profilehelper": ":profilehelper"},
    default_metadata = file_metadata(mode = "0755"),
)

# Kubernetes invokes these commands directly. GNU rm's --one-file-system
# protects nested mounts under the host directory being removed.
_COREUTILS_MEMBERS = [
    "./bin/chmod",
    "./bin/mkdir",
    "./bin/rm",
    "./lib/{triplet}/libc.so.6",
    "./lib/{triplet}/{loader}",
    "./{interpreter}",
    "./lib/{triplet}/libselinux.so.1",
    "./usr/lib/{triplet}/libpcre2-8.so.0",
    "./usr/lib/{triplet}/libpcre2-8.so.0.10.4",
    "./usr/share/doc/coreutils/copyright",
    "./usr/share/doc/libc6/copyright",
    "./usr/share/doc/libselinux1/copyright",
    "./usr/share/doc/libpcre2-8-0/copyright",
    "./usr/share/doc/base-files/copyright",
    "./usr/share/common-licenses/GPL-2",
    "./usr/share/common-licenses/GPL-3",
    "./usr/share/common-licenses/LGPL-2.1",
    "./usr/share/common-licenses/GFDL-1.3",
]

_COREUTILS_PACKAGES = [
    # Common license texts referenced by the runtime packages' notices.
    "@helper_image_packages//base-files:data",
    # mkdir, chmod and rm --one-file-system invoked by the Kubernetes caller.
    "@helper_image_packages//coreutils:data",
    # C runtime and dynamic loader for those three commands.
    "@helper_image_packages//libc6:data",
    # Shared-library dependency of libselinux.
    "@helper_image_packages//libpcre2-8-0:data",
    # Shared-library dependency of the packaged mkdir binary.
    "@helper_image_packages//libselinux1:data",
]

package_layer(
    name = "profile_helper_coreutils_layer",
    out = "profile-helper-coreutils.tar",
    members = _COREUTILS_MEMBERS,
    packages = _COREUTILS_PACKAGES,
)

image_manifest(
    name = "profile_helper_image",
    base = "//images:helper_base",
    cmd = ["/usr/bin/profilehelper"],
    layers = [
        ":profile_helper_coreutils_layer",
        ":profile_helper_layer",
    ],
    tags = ["manual"],
    # The Go binary follows the target platform and must match the image's OS.
    # This constrains the target, not the host or execution platform.
    target_compatible_with = ["@platforms//os:linux"],
)

image_outputs(
    name = "profile_helper",
    image = ":profile_helper_image",
    image_name = "benchmarks/profile-helper",
)

arch_genrule(
    name = "qualification_httpd_archives",
    src = ":httpd_archive",
    template = "qualification_httpd_archive_%s.tar",
)

arch_genrule(
    name = "qualification_httpd_contexts",
    src = ":httpd_context",
    template = "qualification_httpd_context_%s.tar",
)
