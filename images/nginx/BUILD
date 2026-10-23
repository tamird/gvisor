load("@rules_foreign_cc//foreign_cc:defs.bzl", "configure_make")
load("//tools:defs.bzl", "select_arch", "target_emulator")

package(
    default_applicable_licenses = ["//:license"],
    licenses = ["notice"],
)

exports_files([
    "configure_runner.patch",
    "source.BUILD",
])

_EMULATOR = target_emulator()

configure_make(
    name = "nginx_build",
    build_data = _EMULATOR.tools + [
        "configure.sh",
        "run_probe.sh",
    ],
    configure_in_place = True,
    configure_options = [
        "--prefix=/etc/nginx",
        "--conf-path=/etc/nginx/nginx.conf",
        "--error-log-path=stderr",
        "--pid-path=/tmp/nginx.pid",
        "--lock-path=/tmp/nginx.lock",
        "--http-log-path=/dev/stdout",
        "--http-client-body-temp-path=/tmp/client_body",
        "--http-proxy-temp-path=/tmp/proxy",
        "--http-fastcgi-temp-path=/tmp/fastcgi",
        "--http-uwsgi-temp-path=/tmp/uwsgi",
        "--http-scgi-temp-path=/tmp/scgi",
        "--user=nginx",
        "--group=nginx",
        # The basic fixture serves static HTTP files. It does not exercise
        # authentication, compression or rewriting and needs no crypt/zlib/PCRE.
        "--without-http_auth_basic_module",
        "--without-http_gzip_module",
        "--without-http_rewrite_module",
    ],
    configure_prefix = '"$$EXT_BUILD_ROOT$$/$(execpath configure.sh)" ' + select_arch(
        amd64 = 'x86_64 "$$EXT_BUILD_ROOT$$/$(execpath //tools/bazeldefs:qemu_x86_64)" ',
        arm64 = 'aarch64 "$$EXT_BUILD_ROOT$$/$(execpath //tools/bazeldefs:qemu_aarch64)" ',
    ) + '"$$EXT_BUILD_ROOT$$/$(execpath run_probe.sh)"',
    # Configure runs Linux feature probes, using QEMU for the other target CPU.
    exec_compatible_with = ["@platforms//os:linux"],
    lib_source = "@nginx_source//:source",
    linkopts = ["-static"],
    out_binaries = ["nginx"],
    out_data_files = ["ngx_auto_config.h"],
    out_include_dir = "",
    # Stage the executable and configure results; image configuration and data
    # are separate layers. Upstream install would populate host-absolute paths.
    postfix_script = """mkdir -p "$$INSTALLDIR/bin"
cp objs/nginx "$$INSTALLDIR/bin/nginx"
cp objs/ngx_auto_config.h "$$INSTALLDIR/ngx_auto_config.h"
""",
    set_file_prefix_map = True,
    target_compatible_with = [
        "@llvm//constraints/libc:musl",
        "@platforms//os:linux",
    ],
    targets = [""],
)

filegroup(
    name = "binary",
    srcs = [":nginx_build"],
    output_group = "nginx",
    visibility = ["//images/basic:__pkg__"],
)
