"""Declared AMD64 programs for the original foreign_cc plugin build."""

# Names used by the foreign_cc framework, TLDK and DPDK Makefiles. The
# archive's dispatch depends on argv[0], so each name is a distinct input.
_APPLETS = [
    "awk", "basename", "cat", "chmod", "cmp", "cp", "cut", "date", "diff",
    "dirname", "echo", "env", "expr", "false", "find", "getconf", "grep",
    "head", "install", "ln", "ls", "mkdir", "mktemp", "mv", "nproc", "od",
    "paste", "printf", "pwd", "readlink", "realpath", "rm", "rmdir", "sed",
    "sha256sum", "sleep", "sort", "tail", "tee", "test", "touch", "tr",
    "true", "uname", "uniq", "wc", "which", "xargs",
]

def _programs(ctx):
    ctx.download_and_extract(
        url = "https://deb.debian.org/debian/pool/main/b/bash/bash-static_5.2.37-2+b10_amd64.deb",
        sha256 = "a8dfc61a50e70cdf2b790dcd9e1c45e2e42f396988e7fed1c45b1444df932953",
        type = "deb",
    )
    ctx.extract("data.tar.xz", output = "payload")
    ctx.symlink("payload/usr/bin/bash-static", "bin/bash")
    ctx.symlink("payload/usr/bin/bash-static", "bin/sh")
    for name in _APPLETS:
        ctx.symlink(ctx.path(ctx.attr.busybox), "bin/" + name)

    # DPDK's build-only ELF reader includes the BSD queue macros through rte_pci.h.
    # Keep this header out of the GNU target's compiler search path.
    ctx.download(
        url = "https://raw.githubusercontent.com/alpinelinux/aports/988f183cc9d6699930c3e18ccf4a9e36010afb56/main/libc-dev/sys-queue.h",
        sha256 = "c13407edd0e33be73cae72514cb234f8612e1c0e54401c9448daffd3a240158b",
        output = "include/sys/queue.h",
    )
    ctx.file("BUILD.bazel", """load("@rules_cc//cc:cc_library.bzl", "cc_library")

exports_files(["bin/bash"])

cc_library(
    name = "bsd_queue",
    hdrs = ["include/sys/queue.h"],
    includes = ["include"],
    visibility = ["//visibility:public"],
)

filegroup(
    name = "utilities",
    srcs = glob(["bin/*"], exclude = ["bin/bash"]),
    visibility = ["//visibility:public"],
)
""")

plugin_programs = repository_rule(
    implementation = _programs,
    attrs = {"busybox": attr.label(mandatory = True, allow_single_file = True)},
)
