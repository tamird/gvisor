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
    # Both Noble packages are pinned in gVisor's apt lock.
    package = "pool/main/b/busybox/busybox-static_1.36.1-6ubuntu3.1_%s.deb" % ctx.attr.arch
    ctx.download_and_extract(
        url = [
            "https://snapshot.ubuntu.com/ubuntu/20260928T000000Z/" + package,
            ctx.attr.mirror + "/" + package,
        ],
        sha256 = ctx.attr.sha256,
        type = "deb",
    )
    ctx.extract("data.tar.zst", output = "payload")
    ctx.file("BUILD.bazel", 'exports_files(["payload/usr/bin/busybox"])\n')

busybox_repository = repository_rule(
    implementation = _archive_impl,
    attrs = {
        "arch": attr.string(mandatory = True),
        "mirror": attr.string(mandatory = True),
        "sha256": attr.string(mandatory = True),
    },
)
