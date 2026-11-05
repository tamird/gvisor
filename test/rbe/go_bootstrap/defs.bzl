"""Qualification-only fixture for rules_shell's proposed exec toolchain."""

load("@rules_shell//shell/toolchains:sh_exec_toolchain.bzl", "ShExecToolchainInfo")

def _shell_toolchain_impl(ctx):
    return [platform_common.ToolchainInfo(
        sh_exec_toolchain = ShExecToolchainInfo(
            interpreter = ctx.attr.interpreter[DefaultInfo].files_to_run,
            max_command_length = 64000,
        ),
    )]

# This is the same FilesToRunProvider seam exercised by rules_shell#64.
shell_toolchain = rule(
    implementation = _shell_toolchain_impl,
    attrs = {"interpreter": attr.label(mandatory = True, executable = True, cfg = "exec")},
)

def _archive_impl(ctx):
    # This is the exact Noble AMD64 package already pinned in gVisor's apt lock.
    ctx.download_and_extract(
        url = "https://snapshot.ubuntu.com/ubuntu/20260928T000000Z/pool/main/b/busybox/busybox-static_1.36.1-6ubuntu3.1_amd64.deb",
        sha256 = "944b2728f53ceb3916cec2c962873c9951e612408099601751db2a0a5d81e0ed",
        type = "deb",
    )
    ctx.extract("data.tar.zst", output = "payload")
    ctx.file("BUILD.bazel", 'exports_files(["payload/usr/bin/busybox"])\n')

busybox_repository = repository_rule(implementation = _archive_impl)
