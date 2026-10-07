# Copyright 2026 The gVisor Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Full-system ARM64 tests with declared QEMU and guest inputs."""

load("@bazel_skylib//lib:shell.bzl", "shell")
load("//tools:defs.bzl", "pkg_tar")
load("//tools/bazeldefs:defs.bzl", "arch_config", "arm64_config", "transition_allowlist")

_guest_transition = transition(implementation = arm64_config, inputs = [], outputs = arch_config)

TcgImageInfo = provider(
    doc = "Declared kernel and initramfs for a full-system test guest.",
    fields = ["kernel", "initramfs"],
)

def _image_impl(ctx):
    tar = ctx.toolchains["@tar.bzl//tar/toolchain:target_type"]
    kernel = ctx.actions.declare_file(ctx.label.name + ".Image")
    initramfs = ctx.actions.declare_file(ctx.label.name + ".cpio.gz")
    ctx.actions.run(
        executable = ctx.executable._builder,
        arguments = [
            tar.tarinfo.binary.path,
            ctx.file.host_tools.path,
            ctx.file.guest.path,
            ctx.file.init.path,
            ctx.attr.kernel_release,
            kernel.path,
            initramfs.path,
        ],
        inputs = [ctx.file.host_tools, ctx.file.guest, ctx.file.init],
        tools = [ctx.attr._builder[DefaultInfo].files_to_run, tar.tarinfo.binary],
        outputs = [kernel, initramfs],
        env = tar.tarinfo.default_env,
        mnemonic = "TcgGuestImage",
        progress_message = "Preparing the ARM64 64K guest",
    )
    return [
        DefaultInfo(files = depset([kernel, initramfs])),
        TcgImageInfo(kernel = kernel, initramfs = initramfs),
    ]

tcg_image = rule(
    implementation = _image_impl,
    attrs = {
        "host_tools": attr.label(allow_single_file = True, mandatory = True),
        "guest": attr.label(allow_single_file = True, cfg = _guest_transition, mandatory = True),
        "_allowlist_function_transition": attr.label(default = transition_allowlist),
        "init": attr.label(allow_single_file = True, mandatory = True),
        "kernel_release": attr.string(mandatory = True),
        "_builder": attr.label(default = Label(":build_image"), executable = True, cfg = "exec"),
    },
    toolchains = ["@tar.bzl//tar/toolchain:target_type"],
)

def _tcg_test_impl(ctx):
    # rules_pkg 1.0.1 packages runfiles.files, not custom runfiles aliases.
    # The syscall runner uses file-based runfiles, including release sidecars.
    payload_runfiles = ctx.attr.payload[DefaultInfo].default_runfiles
    if payload_runfiles.symlinks.to_list() or payload_runfiles.root_symlinks.to_list():
        fail("TCG payload requires runfiles aliases unsupported by pkg_tar")
    image = ctx.attr._image[TcgImageInfo]
    tar = ctx.toolchains["@tar.bzl//tar/toolchain:target_type"]
    executable = ctx.actions.declare_file(ctx.label.name + ".sh")
    inputs = [ctx.file.archive, ctx.file._host_tools, image.kernel, image.initramfs, ctx.executable._launcher, tar.tarinfo.binary]
    arguments = [
        ctx.file._host_tools.short_path,
        image.kernel.short_path,
        image.initramfs.short_path,
        ctx.file.archive.short_path,
        ctx.executable.payload.short_path,
        tar.tarinfo.binary.short_path,
        str(ctx.attr.payload.label),
    ]
    ctx.actions.write(
        executable,
        "#!/bin/bash\nset -euo pipefail\n" +
        "cd \"${TEST_SRCDIR}/${TEST_WORKSPACE}\"\n" +
        "exec %s %s \"$@\"\n" % (shell.quote(ctx.executable._launcher.short_path), " ".join([shell.quote(arg) for arg in arguments])),
        is_executable = True,
    )
    runfiles = ctx.runfiles(files = inputs, transitive_files = tar.default.files).merge(ctx.attr._launcher[DefaultInfo].default_runfiles)
    return [
        DefaultInfo(executable = executable, runfiles = runfiles),
        testing.TestEnvironment(tar.tarinfo.default_env),
    ]

_tcg_test = rule(
    implementation = _tcg_test_impl,
    test = True,
    attrs = {
        "payload": attr.label(executable = True, cfg = "target", mandatory = True),
        "archive": attr.label(allow_single_file = True, mandatory = True),
        "_host_tools": attr.label(default = Label("@tcg_host_tools//:flat"), allow_single_file = True),
        "_image": attr.label(default = Label(":guest"), providers = [TcgImageInfo]),
        "_launcher": attr.label(default = Label(":run"), executable = True, cfg = "exec"),
    },
    toolchains = ["@tar.bzl//tar/toolchain:target_type"],
)

def arm64_tcg_test(name, payload, **kwargs):
    """Wraps a declared ARM64 payload, preserving its caller-owned test attributes."""
    pkg_tar(
        name = name + "_payload",
        testonly = True,
        srcs = [payload],
        include_runfiles = True,
        strip_prefix = "/",
        allow_duplicates_with_different_content = False,
        tags = ["manual"],
    )
    _tcg_test(
        name = name,
        payload = payload,
        archive = ":" + name + "_payload",
        exec_compatible_with = ["@platforms//os:linux", "@platforms//cpu:x86_64"],
        exec_properties = {
            "test.EstimatedCPU": "2",
            "test.EstimatedMemory": "6GB",
            "test.EstimatedFreeDiskBytes": "8GB",
            "test.dockerUser": "nobody",
            "test.nonroot-workspace": "true",
            "test.workload-isolation-type": "oci",
        },
        tags = ["manual", "no-local", "arm64-64k-tcg"],
        **kwargs
    )
