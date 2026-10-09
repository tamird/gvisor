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

"""Full-system tests with declared QEMU and guest inputs."""

load("@bazel_skylib//lib:shell.bzl", "shell")
load("//tools:arch.bzl", "amd64_config", "arch_config", "arm64_config", "transition_allowlist")
load("//tools:defs.bzl", "pkg_tar")
load("//tools/bazeldefs:platforms.bzl", "RBE_DOCKER_TOOLS_IMAGE")

def _guest_config(settings, attr):
    if attr.architecture == "amd64":
        return amd64_config(settings, attr)
    return arm64_config(settings, attr)

_guest_transition = transition(implementation = _guest_config, inputs = [], outputs = arch_config)

TcgImageInfo = provider(
    doc = "Declared kernel and initramfs for a full-system test guest.",
    fields = ["kernel", "initramfs", "architecture"],
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
            str(ctx.attr.page_size),
            ctx.executable._zstd.path,
            ctx.attr.architecture,
            " ".join(ctx.attr.kernel_modules),
        ] + [archive.path for archive in ctx.files.kernel_archives],
        inputs = [ctx.file.host_tools, ctx.file.guest, ctx.file.init] + ctx.files.kernel_archives,
        tools = [ctx.attr._builder[DefaultInfo].files_to_run, ctx.attr._zstd[DefaultInfo].files_to_run, tar.tarinfo.binary],
        outputs = [kernel, initramfs],
        env = tar.tarinfo.default_env,
        mnemonic = "TcgGuestImage",
        progress_message = "Preparing the %s guest with %s" % (ctx.attr.architecture, ctx.attr.kernel_release),
    )
    return [
        DefaultInfo(files = depset([kernel, initramfs])),
        TcgImageInfo(kernel = kernel, initramfs = initramfs, architecture = ctx.attr.architecture),
    ]

tcg_image = rule(
    implementation = _image_impl,
    attrs = {
        "host_tools": attr.label(allow_single_file = True, mandatory = True),
        "guest": attr.label(allow_single_file = True, cfg = _guest_transition, mandatory = True),
        "_allowlist_function_transition": attr.label(default = transition_allowlist),
        "init": attr.label(allow_single_file = True, mandatory = True),
        "kernel_release": attr.string(mandatory = True),
        "kernel_archives": attr.label_list(allow_files = True),
        "architecture": attr.string(default = "arm64", values = ["amd64", "arm64"]),
        "kernel_modules": attr.string_list(default = ["9p", "9pnet_virtio", "overlay"]),
        "page_size": attr.int(default = 65536, values = [4096, 65536]),
        "_zstd": attr.label(default = Label("@llvm_zstd//:zstd_cli"), executable = True, cfg = "exec"),
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
    image = ctx.attr.image[TcgImageInfo]
    architecture = {"arm64_tcg": "arm64", "amd64_kvm": "amd64"}[ctx.attr.machine]
    if image.architecture != architecture:
        fail("Guest image architecture %s does not match %s" % (image.architecture, ctx.attr.machine))
    tar = ctx.toolchains["@tar.bzl//tar/toolchain:target_type"]
    executable = ctx.actions.declare_file(ctx.label.name + ".sh")
    inputs = [ctx.file.archive, ctx.file.host_tools, image.kernel, image.initramfs, ctx.executable._launcher, ctx.file._test_setup, tar.tarinfo.binary]
    arguments = [
        ctx.file.host_tools.short_path,
        image.kernel.short_path,
        image.initramfs.short_path,
        ctx.file.archive.short_path,
        ctx.executable.payload.short_path,
        tar.tarinfo.binary.short_path,
        str(ctx.attr.payload.label),
        ctx.attr.machine,
        ctx.file._test_setup.short_path,
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
        "host_tools": attr.label(default = Label("@tcg_host_tools//:flat"), allow_single_file = True),
        "machine": attr.string(default = "arm64_tcg", values = ["arm64_tcg", "amd64_kvm"]),
        "image": attr.label(default = Label(":guest"), providers = [TcgImageInfo]),
        "_launcher": attr.label(default = Label(":run"), executable = True, cfg = "exec"),
        "_test_setup": attr.label(default = Label("@bazel_tools//tools/test:test_setup"), allow_single_file = True),
    },
    toolchains = ["@tar.bzl//tar/toolchain:target_type"],
)

def _guest_test(name, payload, tags, image, **kwargs):
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
        image = image,
        archive = ":" + name + "_payload",
        exec_compatible_with = ["@platforms//os:linux", "@platforms//cpu:x86_64"],
        tags = tags + ["manual"],
        **kwargs
    )

def arm64_tcg_test(name, payload, tags, image = Label(":guest"), **kwargs):
    """Wraps an ARM64 payload for unprivileged emulation on an AMD64 worker."""
    _guest_test(
        name = name,
        payload = payload,
        image = image,
        tags = tags + ["no-local"],
        exec_properties = {
            "test.EstimatedCPU": "2",
            "test.EstimatedMemory": "6GB",
            "test.EstimatedFreeDiskBytes": "8GB",
            # Bazel's undeclared-output collection requires file and zip.
            "test.container-image": RBE_DOCKER_TOOLS_IMAGE,
            "test.dockerUser": "nobody",
            "test.nonroot-workspace": "true",
            # Preserve the test deadline while allowing stopped-guest recovery
            # and Bazel's undeclared-output packaging before forced termination.
            "test.termination-grace-period": "30s",
            "test.workload-isolation-type": "oci",
        },
        **kwargs
    )

def amd64_kvm_test(name, payload, tags, image, **kwargs):
    """Wraps an AMD64 payload for a host with nested KVM, keeping builds remote."""
    _guest_test(
        name = name,
        payload = payload,
        image = image,
        machine = "amd64_kvm",
        host_tools = "@kvm_host_tools//:flat",
        # Only the VM process needs the host device. Its declared inputs and
        # payload remain ordinary remotely executable build actions.
        tags = tags + ["no-remote-exec", "no-sandbox"],
        **kwargs
    )
