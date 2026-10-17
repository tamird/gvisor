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
load("@tar.bzl//tar:tar.bzl", "tar_lib")
load("//tools:arch.bzl", "amd64_config", "arch_config", "arm64_config", "transition_allowlist")
load("//tools:defs.bzl", "pkg_tar")
load("//tools/bazeldefs:platforms.bzl", "RBE_DOCKER_TOOLS_IMAGE")

def _guest_config(settings, attr):
    if attr.architecture == "amd64":
        return amd64_config(settings, attr)
    return arm64_config(settings, attr)

_guest_transition = transition(implementation = _guest_config, inputs = [], outputs = arch_config)

TcgImageInfo = provider(
    doc = "Declared kernel and root filesystem for a full-system test guest.",
    fields = ["kernel", "rootfs", "architecture"],
)

def _host_tools_impl(ctx):
    tar = ctx.toolchains[tar_lib.toolchain_type]
    coreutils = ctx.toolchains["@bazel_lib//lib:coreutils_toolchain_type"]
    directory = ctx.actions.declare_directory(ctx.label.name)
    args = ctx.actions.args()
    args.add(coreutils.coreutils_info.bin)
    args.add(directory.path)
    args.add(tar.tarinfo.binary.path)
    args.add(ctx.file.archive.path)
    args.add_all(ctx.attr.members, format_each = "./%s")
    ctx.actions.run_shell(
        command = '"$1" mkdir -p "$2" && "$3" -xpf "$4" -C "$2" --no-same-owner "${@:5}"',
        arguments = [args],
        inputs = [ctx.file.archive],
        tools = [coreutils.default.files, tar.default.files],
        outputs = [directory],
        env = tar.tarinfo.default_env,
        mnemonic = "GuestHostTools",
        progress_message = "Extracting declared guest host tools %{label}",
    )
    return [DefaultInfo(files = depset([directory]))]

guest_host_tools = rule(
    implementation = _host_tools_impl,
    doc = "Extracts the immutable runtime tool closure once for all guest tests.",
    attrs = {
        "archive": attr.label(allow_single_file = True, mandatory = True),
        "members": attr.string_list(mandatory = True),
    },
    toolchains = [tar_lib.toolchain_type, "@bazel_lib//lib:coreutils_toolchain_type"],
)

def _image_impl(ctx):
    tar = ctx.toolchains["@tar.bzl//tar/toolchain:target_type"]
    kernel = ctx.actions.declare_file(ctx.label.name + ".Image")
    rootfs = ctx.actions.declare_file(ctx.label.name + ".ext4")
    ctx.actions.run(
        executable = ctx.executable._builder,
        arguments = [
            tar.tarinfo.binary.path,
            ctx.file.host_tools.path,
            ctx.file.guest.path,
            ctx.file.init.path,
            ctx.attr.kernel_release,
            kernel.path,
            rootfs.path,
            str(ctx.attr.page_size),
            ctx.executable._zstd.path,
            ctx.attr.architecture,
            ctx.file._ext4.path,
        ] + [archive.path for archive in ctx.files.kernel_archives],
        inputs = [ctx.file.host_tools, ctx.file.guest, ctx.file.init, ctx.file._ext4] + ctx.files.kernel_archives,
        tools = [ctx.attr._builder[DefaultInfo].files_to_run, ctx.attr._zstd[DefaultInfo].files_to_run, tar.tarinfo.binary],
        outputs = [kernel, rootfs],
        env = tar.tarinfo.default_env,
        mnemonic = "TcgGuestImage",
        progress_message = "Preparing the %s guest with %s" % (ctx.attr.architecture, ctx.attr.kernel_release),
    )
    return [
        DefaultInfo(files = depset([kernel, rootfs])),
        TcgImageInfo(kernel = kernel, rootfs = rootfs, architecture = ctx.attr.architecture),
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
        "page_size": attr.int(default = 65536, values = [4096, 65536]),
        "_ext4": attr.label(default = Label(":ext4.sh"), allow_single_file = True),
        "_zstd": attr.label(default = Label("@llvm_zstd//:zstd_cli"), executable = True, cfg = "exec"),
        "_builder": attr.label(default = Label(":build_image"), executable = True, cfg = "exec"),
    },
    toolchains = ["@tar.bzl//tar/toolchain:target_type"],
)

def _payload_image_impl(ctx):
    tar = ctx.toolchains[tar_lib.toolchain_type]
    payload = ctx.executable.payload.short_path
    payload_image = ctx.actions.declare_file(ctx.label.name + ".ext4")
    ctx.actions.run(
        executable = ctx.executable._payload_builder,
        arguments = [
            tar.tarinfo.binary.path,
            ctx.file._image_host_tools.path,
            ctx.file.archive.path,
            payload_image.path,
            ctx.file._ext4.path,
            # test-setup.sh resolves the executable inside its runfiles tree.
            "%s.runfiles/%s/%s" % (payload, ctx.workspace_name, payload),
        ],
        inputs = [ctx.file._image_host_tools, ctx.file.archive, ctx.file._ext4],
        tools = [ctx.attr._payload_builder[DefaultInfo].files_to_run, tar.tarinfo.binary],
        outputs = [payload_image],
        env = tar.tarinfo.default_env,
        mnemonic = "GuestPayloadImage",
        progress_message = "Preparing guest payload filesystem %{label}",
    )
    return [DefaultInfo(files = depset([payload_image]))]

_payload_image = rule(
    implementation = _payload_image_impl,
    attrs = {
        "payload": attr.label(executable = True, cfg = "target", mandatory = True),
        "archive": attr.label(allow_single_file = True, mandatory = True),
        "_image_host_tools": attr.label(default = Label("@tcg_host_tools//:flat"), allow_single_file = True, cfg = "exec"),
        "_ext4": attr.label(default = Label(":ext4.sh"), allow_single_file = True),
        "_payload_builder": attr.label(default = Label(":build_payload"), executable = True, cfg = "exec"),
    },
    toolchains = [tar_lib.toolchain_type],
)

def _tcg_test_impl(ctx):
    # rules_pkg 1.0.1 packages runfiles.files, not custom runfiles aliases.
    # The syscall runner uses file-based runfiles, including release sidecars.
    executables = [ctx.attr.payload] + ([ctx.attr.run_under] if ctx.attr.run_under else [])
    for dependency in executables:
        runfiles = dependency[DefaultInfo].default_runfiles
        if runfiles.symlinks.to_list() or runfiles.root_symlinks.to_list():
            fail("Guest executable %s requires runfiles aliases unsupported by pkg_tar" % dependency.label)
    image = ctx.attr.image[TcgImageInfo]
    architecture = {"arm64_tcg": "arm64", "amd64_kvm": "amd64"}[ctx.attr.machine]
    if image.architecture != architecture:
        fail("Guest image architecture %s does not match %s" % (image.architecture, ctx.attr.machine))
    if ctx.attr.arm64_el2 and architecture != "arm64":
        fail("EL2 exposure requires an ARM64 guest")
    executable = ctx.actions.declare_file(ctx.label.name + ".sh")
    configuration = ctx.actions.declare_file(ctx.label.name + ".guest.sh")
    if ctx.attr.cpus <= 0 or ctx.attr.memory_mib <= 0 or ctx.attr.scratch_gib <= 0:
        fail("Guest CPU count, memory and scratch capacity must be positive")
    if ctx.attr.smt and ctx.attr.cpus != 2:
        fail("SMT guests require exactly two sibling CPUs")
    environment = {}
    inherited_environment = []
    if RunEnvironmentInfo in ctx.attr.payload:
        payload_environment = ctx.attr.payload[RunEnvironmentInfo]
        environment.update(payload_environment.environment)
        inherited_environment = payload_environment.inherited_environment
    environment.update(ctx.attr.guest_env)
    ctx.actions.write(
        configuration,
        "guest_memory_mib=%d\nguest_scratch_gib=%d\nguest_run_under=%s\nguest_emulator=%s\nguest_arm64_el2=%s\nguest_smp=%s\n" % (
            ctx.attr.memory_mib,
            ctx.attr.scratch_gib,
            shell.quote(ctx.executable.run_under.short_path if ctx.executable.run_under else ""),
            shell.quote(ctx.file.emulator.short_path if ctx.file.emulator != None else ""),
            "true" if ctx.attr.arm64_el2 else "false",
            shell.quote("2,sockets=1,cores=1,threads=2" if ctx.attr.smt else str(ctx.attr.cpus)),
        ) +
        "guest_env=(%s)\nguest_inherited_env=(%s)\n" % (
            " ".join([shell.quote(key + "=" + value) for key, value in environment.items()]),
            " ".join([shell.quote(key) for key in inherited_environment]),
        ),
    )
    inputs = [ctx.file.payload_image, ctx.file.host_tools, image.kernel, image.rootfs, configuration, ctx.executable._launcher, ctx.file._test_setup]
    if ctx.file.emulator != None:
        inputs.append(ctx.file.emulator)
    arguments = [
        ctx.file.host_tools.short_path,
        image.kernel.short_path,
        image.rootfs.short_path,
        ctx.file.payload_image.short_path,
        ctx.executable.payload.short_path,
        str(ctx.attr.payload.label),
        ctx.attr.machine,
        ctx.file._test_setup.short_path,
    ]
    ctx.actions.write(
        executable,
        "#!/bin/bash\nset -euo pipefail\n" +
        "cd \"${TEST_SRCDIR}/${TEST_WORKSPACE}\"\n" +
        "export GVISOR_TEST_GUEST_CONFIG=%s\n" % shell.quote(configuration.short_path) +
        "exec %s %s \"$@\"\n" % (shell.quote(ctx.executable._launcher.short_path), " ".join([shell.quote(arg) for arg in arguments])),
        is_executable = True,
    )
    runfiles = ctx.runfiles(files = inputs).merge(ctx.attr._launcher[DefaultInfo].default_runfiles)
    return [
        DefaultInfo(executable = executable, runfiles = runfiles),
        RunEnvironmentInfo(environment = {}, inherited_environment = inherited_environment),
    ]

_tcg_test = rule(
    implementation = _tcg_test_impl,
    test = True,
    attrs = {
        "payload": attr.label(executable = True, cfg = "target", mandatory = True),
        "payload_image": attr.label(allow_single_file = True, mandatory = True),
        "host_tools": attr.label(default = Label(":host_tools"), allow_single_file = True, cfg = "exec"),
        "emulator": attr.label(allow_single_file = True, cfg = "exec"),
        "machine": attr.string(default = "arm64_tcg", values = ["arm64_tcg", "amd64_kvm"]),
        "arm64_el2": attr.bool(default = False),
        "smt": attr.bool(default = False),
        "image": attr.label(default = Label(":guest"), providers = [TcgImageInfo]),
        "run_under": attr.label(executable = True, cfg = "target"),
        "guest_env": attr.string_dict(),
        "cpus": attr.int(default = 2),
        "memory_mib": attr.int(default = 3072),
        "scratch_gib": attr.int(default = 4),
        "_launcher": attr.label(default = Label(":run"), executable = True, cfg = "exec"),
        "_test_setup": attr.label(default = Label("@bazel_tools//tools/test:test_setup"), allow_single_file = True),
    },
)

def _guest_test(name, payload, tags, image, run_under = None, **kwargs):
    pkg_tar(
        name = name + "_payload",
        testonly = True,
        srcs = [payload] + ([run_under] if run_under else []),
        include_runfiles = True,
        strip_prefix = "/",
        allow_duplicates_with_different_content = False,
        tags = ["manual"],
    )

    # Image construction remains remote even when the guest test needs KVM on
    # the local worker. Test execution tags must not constrain build actions.
    _payload_image(
        name = name + "_payload_image",
        testonly = True,
        payload = payload,
        archive = ":" + name + "_payload",
        exec_compatible_with = ["@platforms//os:linux", "@platforms//cpu:x86_64"],
        tags = ["manual"],
    )
    _tcg_test(
        name = name,
        payload = payload,
        image = image,
        run_under = run_under,
        payload_image = ":" + name + "_payload_image",
        exec_compatible_with = ["@platforms//os:linux", "@platforms//cpu:x86_64"],
        tags = tags + ["manual"],
        **kwargs
    )

def arm64_tcg_test(name, payload, tags, image = Label(":guest"), cpus = 2, **kwargs):
    """Wraps an ARM64 payload for unprivileged emulation on an AMD64 worker."""

    _guest_test(
        name = name,
        payload = payload,
        image = image,
        emulator = Label("//test/rbe/tcg/qemu:system_aarch64"),
        cpus = cpus,
        tags = tags + ["no-local"],
        exec_properties = {
            "test.EstimatedCPU": str(cpus),
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
        host_tools = Label(":kvm_host_tools"),
        # Only the VM process needs the host device. Its declared inputs and
        # payload remain ordinary remotely executable build actions.
        tags = tags + ["no-remote-exec", "no-sandbox"],
        **kwargs
    )
