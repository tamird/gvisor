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
    ctx.file("BUILD.bazel", """exports_files(["bin/bash"])

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
