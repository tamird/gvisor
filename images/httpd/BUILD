load("@rules_foreign_cc//foreign_cc:defs.bzl", "configure_make")
load("@rules_img//img:layer.bzl", "file_metadata", "image_layer")
load("//images:busybox.bzl", host_compiler = "busybox_host_compiler")
load("//tools:defs.bzl", "target_emulator")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

exports_files([
    "apr-target-runner.patch",
    "apr-util-target-runner.patch",
    "httpd-target-runner.patch",
    "pcre2-flags.patch",
    "source.BUILD",
    "ports.json",
])

_EMULATOR = target_emulator()

# Configure and the target generator share the exact emulator command.
_PREFIX = 'HTTPD_EMULATOR="' + _EMULATOR.cmd + '" '

_TARGET = select({
    "@platforms//cpu:aarch64": ["--host=aarch64-linux-musl"],
    "@platforms//cpu:x86_64": ["--host=x86_64-linux-musl"],
})

_COMMON_ENV = {
    "HTTPD_RUNNER": "$$EXT_BUILD_ROOT$$/$(execpath run_target.sh)",
    # Keep upstream build banners independent of the worker's wall clock.
    "SOURCE_DATE_EPOCH": "0",
}

_ARM_ENV = _COMMON_ENV | {"HTTPD_TARGET_ARCH": "aarch64"}
_AMD_ENV = _COMMON_ENV | {"HTTPD_TARGET_ARCH": "x86_64"}

_HOST_ENV = {"CC_FOR_BUILD": "$$EXT_BUILD_ROOT$$/$(execpath :hostcc)"}

# foreign_cc already forwards native CcInfo include/library paths. The
# upstream Bazel library is libpcre2.a, not libpcre2-8.a.
_PCRE_ENV = {
    "PCRE2_CFLAGS": "",
    "PCRE2_LIBS": "-lpcre2",
}

_TOOLS = ["run_target.sh"] + _EMULATOR.tools

# The real configure probes exercise Linux behavior, including under QEMU.
_EXEC_CONSTRAINTS = ["@platforms//os:linux"]

_TARGET_CONSTRAINTS = [
    "@llvm//constraints/libc:musl",
    "@platforms//os:linux",
]

# APR's upstream CC_FOR_BUILD hook needs the execution C toolchain, just as
# BusyBox's HOSTCC does. Reuse its complete flags/environment/input adapter.
host_compiler(name = "hostcc")

configure_make(
    name = "apr",
    build_data = _TOOLS + [":hostcc"],
    configure_in_place = True,
    configure_options = [
        "--disable-dso",
        "--disable-shared",
        "--enable-static",
    ] + _TARGET,
    configure_prefix = _PREFIX,
    env = select({
        "@platforms//cpu:aarch64": _ARM_ENV | _HOST_ENV,
        "@platforms//cpu:x86_64": _AMD_ENV | _HOST_ENV,
    }),
    exec_compatible_with = _EXEC_CONSTRAINTS,
    lib_name = "apr",
    lib_source = "@apr_source//:source",
    linkopts = ["-static"],
    make_prefix = _PREFIX,
    out_data_files = [
        "apr.h",
        "apr_private.h",
        "config.log",
    ],
    out_static_libs = ["libapr-1.a"],
    postfix_script = """cp include/apr.h "$$INSTALLDIR/apr.h"
cp include/arch/unix/apr_private.h "$$INSTALLDIR/apr_private.h"
cp config.log "$$INSTALLDIR/config.log"
""",
    set_file_prefix_map = True,
    target_compatible_with = _TARGET_CONSTRAINTS,
)

configure_make(
    name = "apr_util",
    build_data = _TOOLS,
    configure_in_place = True,
    configure_options = [
        "--disable-shared",
        "--disable-util-dso",
        "--enable-static",
        "--with-apr=$$EXT_BUILD_DEPS$$/apr",
        "--with-dbm=sdbm",
        "--with-expat=yes",
        "--without-apr-iconv",
        "--without-berkeley-db",
        "--without-crypto",
        "--without-gdbm",
        "--without-iconv",
        "--without-ldap",
        "--without-mysql",
        "--without-ndbm",
        "--without-odbc",
        "--without-oracle",
        "--without-pgsql",
        "--without-sqlite2",
        "--without-sqlite3",
    ] + _TARGET,
    configure_prefix = _PREFIX,
    env = select({
        "@platforms//cpu:aarch64": _ARM_ENV,
        "@platforms//cpu:x86_64": _AMD_ENV,
    }),
    exec_compatible_with = _EXEC_CONSTRAINTS,
    lib_name = "apr_util",
    lib_source = "@apr_util_source//:source",
    linkopts = ["-static"],
    make_prefix = _PREFIX,
    out_data_files = [
        "apu_config.h",
        "config.log",
    ],
    out_static_libs = ["libaprutil-1.a"],
    postfix_script = """cp include/private/apu_config.h "$$INSTALLDIR/apu_config.h"
cp config.log "$$INSTALLDIR/config.log"
""",
    set_file_prefix_map = True,
    target_compatible_with = _TARGET_CONSTRAINTS,
    deps = [
        ":apr",
        "@libexpat//:libexpat",
    ],
)

configure_make(
    name = "httpd",
    args = [
        "-f",
        "Makefile",
        "-f",
        '"$$EXT_BUILD_ROOT$$/$(execpath httpd.mk)"',
    ],
    build_data = _TOOLS,
    configure_in_place = True,
    configure_options = [
        # Runtime paths must not contain foreign_cc's temporary install root.
        "--prefix=/usr/local/apache2",
        "--disable-so",
        "--enable-modules=none",
        "--enable-mods-static='authz_core authz_host autoindex deflate dir filter log_config logio mime reqtimeout unixd'",
        "--with-apr=$$EXT_BUILD_DEPS$$/apr",
        "--with-apr-util=$$EXT_BUILD_DEPS$$/apr_util",
        "--with-mpm=event",
        # Use only the declared zlib provider, not a worker installation.
        "--with-z=$$EXT_BUILD_DEPS$$",
    ] + _TARGET,
    configure_prefix = _PREFIX,
    data = ["httpd.mk"],
    env = select({
        "@platforms//cpu:aarch64": _ARM_ENV | _PCRE_ENV,
        "@platforms//cpu:x86_64": _AMD_ENV | _PCRE_ENV,
    }),
    exec_compatible_with = _EXEC_CONSTRAINTS,
    lib_source = "@httpd_source//:source",
    linkopts = ["-static"],
    make_prefix = _PREFIX,
    out_binaries = ["httpd"],
    out_data_files = [
        "ap_config_auto.h",
        "ap_config_layout.h",
        "config.log",
    ],
    out_include_dir = "",
    postfix_script = """mkdir -p "$$INSTALLDIR/bin"
cp httpd "$$INSTALLDIR/bin/httpd"
cp include/ap_config_auto.h "$$INSTALLDIR/ap_config_auto.h"
cp include/ap_config_layout.h "$$INSTALLDIR/ap_config_layout.h"
cp config.log "$$INSTALLDIR/config.log"
""",
    set_file_prefix_map = True,
    target_compatible_with = _TARGET_CONSTRAINTS,
    targets = ["bazel-httpd"],
    deps = [
        ":apr",
        ":apr_util",
        "@libexpat//:libexpat",
        "@pcre2//:pcre2",
        "@zlib//:zlib",
    ],
)

filegroup(
    name = "binary",
    srcs = [":httpd"],
    output_group = "httpd",
    visibility = ["//images:__subpackages__"],
)

# One static server and its data are shared by both image-specific configs.
image_layer(
    name = "server_layer",
    srcs = {
        "/usr/local/apache2/bin/httpd": ":binary",
        "/usr/local/apache2/conf/mime.types": "@httpd_source//:docs/conf/mime.types",
        "/usr/share/licenses/apr/LICENSE": "@apr_source//:LICENSE",
        "/usr/share/licenses/apr/NOTICE": "@apr_source//:NOTICE",
        "/usr/share/licenses/apr-util/LICENSE": "@apr_util_source//:LICENSE",
        "/usr/share/licenses/apr-util/NOTICE": "@apr_util_source//:NOTICE",
        "/usr/share/licenses/httpd/LICENSE": "@httpd_source//:LICENSE",
        "/usr/share/licenses/httpd/NOTICE": "@httpd_source//:NOTICE",
        "/usr/share/licenses/libexpat/NOTICE": "libexpat.NOTICE",
        "/usr/share/licenses/pcre2/NOTICE": "pcre2.NOTICE",
        "/usr/share/licenses/zlib/LICENSE": "@zlib//:LICENSE",
    },
    default_metadata = file_metadata(mode = "0644"),
    file_metadata = {"/usr/local/apache2/bin/httpd": file_metadata(mode = "0755")},
    include_runfiles = False,
    visibility = ["//images:__subpackages__"],
)

image_layer(
    name = "accounts_layer",
    srcs = {
        "/etc/group": "group",
        "/etc/passwd": "passwd",
    },
    default_metadata = file_metadata(mode = "0644"),
    visibility = ["//images:__subpackages__"],
)
