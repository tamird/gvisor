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

load("@bazel_skylib//lib:paths.bzl", "paths")
load("@bazel_skylib//lib:shell.bzl", "shell")
load("@rules_pkg//pkg:path.bzl", "safe_short_path")
load("@tar.bzl//tar:mtree.bzl", "mutate")
load("@tar.bzl//tar:tar.bzl", "tar", "tar_lib")
load("//tools:arch.bzl", "amd64_config", "arch_config", "arm64_config", "transition_allowlist")
load("//tools:defs.bzl", "pkg_tar")
load("//tools/bazeldefs:platforms.bzl", "RBE_DOCKER_TOOLS_IMAGE")
load("//tools/bazeldefs:test_architectures.bzl", "TEST_ROUTING_ATTRS", "route_test")

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

def _add_payload_file(entries, destination, file, mode):
    destination = paths.normalize(destination)
    if paths.is_absolute(destination) or destination in [".", ".."] or destination.startswith("../"):
        fail("Guest payload destination escapes its root: %s" % destination)
    if destination in entries:
        previous = entries[destination]
        if previous.file != file or previous.mode != mode:
            fail("Conflicting guest payload destination %s: %s and %s" % (destination, previous.file.path, file.path))
    entries[destination] = struct(file = file, mode = mode)

def _guest_executables(ctx):
    targets = [ctx.attr.payload] + ([ctx.attr.run_under] if ctx.attr.run_under else [])
    for target in targets:
        info = target[DefaultInfo]
        if info.files_to_run.executable.short_path.startswith("../"):
            fail("Guest executable %s must belong to the main repository; external runfile dependencies are supported" % target.label)

        # Both the remote pkg_tar path and local staging use file-based runfiles,
        # including release sidecars. Neither reconstructs custom aliases.
        if info.default_runfiles.symlinks.to_list() or info.default_runfiles.root_symlinks.to_list():
            fail("Guest executable %s requires unsupported runfiles aliases" % target.label)
    return targets

def _runtime_payload_image_impl(ctx):
    entries = {}
    files = []
    targets = _guest_executables(ctx)
    for target in targets:
        info = target[DefaultInfo]
        executable = info.files_to_run.executable
        files.extend([info.files, info.default_runfiles.files])
        for file in depset([executable], transitive = [info.files]).to_list():
            _add_payload_file(entries, safe_short_path(file), file, "0755" if file == executable else "0555")
        runfiles_root = safe_short_path(executable) + ".runfiles/" + ctx.workspace_name
        for file in info.default_runfiles.files.to_list():
            _add_payload_file(entries, runfiles_root + "/" + file.short_path, file, "0755" if file == executable else "0555")

        # The harness invokes this path even when the executable is absent from
        # DefaultInfo.files or default_runfiles.files.
        _add_payload_file(entries, runfiles_root + "/" + executable.short_path, executable, "0755")
        files.append(depset([executable]))
    for destination in entries:
        parts = destination.split("/")
        for end in range(1, len(parts)):
            ancestor = "/".join(parts[:end])
            if ancestor in entries:
                fail("Overlapping guest payload destinations: %s and %s" % (ancestor, destination))
    manifest = ctx.actions.declare_file(ctx.label.name + ".manifest.sh")
    ctx.actions.write(
        manifest,
        "\n".join([
            "stage_payload %s %s %s %s" % (
                shell.quote(entry.file.short_path),
                shell.quote(destination),
                shell.quote(entry.mode),
                "directory" if entry.file.is_directory else "file",
            )
            for destination, entry in sorted(entries.items())
        ]) + "\n",
    )
    executable = ctx.actions.declare_file(ctx.label.name + ".sh")
    ctx.actions.write(
        executable,
        "#!/bin/bash\nset -euo pipefail\n" +
        "exec %s --runfiles %s %s \"$1\" %s %s\n" % (
            shell.quote(ctx.executable._payload_builder.short_path),
            shell.quote(ctx.file.host_tools.short_path),
            shell.quote(manifest.short_path),
            shell.quote(ctx.file._ext4.short_path),
            shell.quote("%s.runfiles/%s/%s" % (safe_short_path(ctx.executable.payload), ctx.workspace_name, ctx.executable.payload.short_path)),
        ),
        is_executable = True,
    )
    runfiles = ctx.runfiles(
        files = [manifest, ctx.file.host_tools, ctx.file._ext4, ctx.executable._payload_builder],
        transitive_files = depset(transitive = files),
    ).merge(ctx.attr._payload_builder[DefaultInfo].default_runfiles)
    return [DefaultInfo(executable = executable, runfiles = runfiles)]

_runtime_payload_image = rule(
    implementation = _runtime_payload_image_impl,
    executable = True,
    attrs = {
        "payload": attr.label(executable = True, cfg = "target", mandatory = True),
        "run_under": attr.label(executable = True, cfg = "target"),
        "host_tools": attr.label(allow_single_file = True, cfg = "exec", mandatory = True),
        "_payload_builder": attr.label(default = Label(":build_payload"), executable = True, cfg = "exec"),
        "_ext4": attr.label(default = Label(":ext4.sh"), allow_single_file = True),
    },
)

def _tcg_test_impl(ctx):
    _guest_executables(ctx)
    image = ctx.attr.image[TcgImageInfo]
    architecture = {"arm64_tcg": "arm64", "amd64_kvm": "amd64"}[ctx.attr.machine]
    if image.architecture != architecture:
        fail("Guest image architecture %s does not match %s" % (image.architecture, ctx.attr.machine))
    if ctx.attr.arm64_el2 and architecture != "arm64":
        fail("EL2 exposure requires an ARM64 guest")
    executable = ctx.actions.declare_file(ctx.label.name + ".sh")
    configuration = ctx.actions.declare_file(ctx.label.name + ".guest.sh")
    if ctx.attr.memory_mib <= 0 or ctx.attr.scratch_gib <= 0:
        fail("Guest memory and scratch capacity must be positive")
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
            shell.quote("2,sockets=1,cores=1,threads=2" if ctx.attr.smt else "2"),
        ) +
        "guest_user=%s\n" % shell.quote(ctx.attr.guest_user) +
        "guest_env=(%s)\nguest_inherited_env=(%s)\n" % (
            " ".join([shell.quote(key + "=" + value) for key, value in environment.items()]),
            " ".join([shell.quote(key) for key in inherited_environment]),
        ),
    )
    payload_input = ctx.file.payload_image if ctx.attr.machine == "arm64_tcg" else ctx.executable.payload_builder
    inputs = [payload_input, ctx.file.host_tools, image.kernel, image.rootfs, configuration, ctx.executable._launcher, ctx.file._test_setup]
    if ctx.file.emulator != None:
        inputs.append(ctx.file.emulator)
    arguments = [
        ctx.file.host_tools.short_path,
        image.kernel.short_path,
        image.rootfs.short_path,
        payload_input.short_path,
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
    if ctx.attr.payload_builder:
        runfiles = runfiles.merge(ctx.attr.payload_builder[DefaultInfo].default_runfiles)
    providers = route_test(ctx, [
        DefaultInfo(executable = executable, runfiles = runfiles),
        RunEnvironmentInfo(environment = {}, inherited_environment = inherited_environment),
    ], executable)
    for provider in providers:
        if type(provider) == "DefaultInfo":
            # Include the local root wrapper and its helper when routing adds
            # them, so guest_inputs still describes the whole launch closure.
            return providers + [OutputGroupInfo(guest_inputs = depset(
                [executable],
                transitive = [provider.default_runfiles.files],
            ))]
    fail("Guest routing did not return DefaultInfo")

_tcg_test = rule(
    implementation = _tcg_test_impl,
    test = True,
    attrs = dict(TEST_ROUTING_ATTRS, **{
        "payload": attr.label(executable = True, cfg = "target", mandatory = True),
        "payload_image": attr.label(allow_single_file = True),
        "payload_builder": attr.label(executable = True, cfg = "target"),
        "host_tools": attr.label(default = Label(":host_tools"), allow_single_file = True, cfg = "exec"),
        "emulator": attr.label(allow_single_file = True, cfg = "exec"),
        "machine": attr.string(default = "arm64_tcg", values = ["arm64_tcg", "amd64_kvm"]),
        "arm64_el2": attr.bool(default = False),
        "smt": attr.bool(default = False),
        "image": attr.label(default = Label(":guest"), providers = [TcgImageInfo]),
        "run_under": attr.label(executable = True, cfg = "target"),
        "guest_env": attr.string_dict(),
        "guest_user": attr.string(default = "root", values = ["root", "nobody"]),
        "memory_mib": attr.int(default = 3072),
        "scratch_gib": attr.int(default = 4),
        "_launcher": attr.label(default = Label(":run"), executable = True, cfg = "exec"),
        "_test_setup": attr.label(default = Label("@bazel_tools//tools/test:test_setup"), allow_single_file = True),
    }),
)

def _guest_test(name, payload, tags, image, run_under = None, preserve_runfiles_symlinks = False, **kwargs):
    srcs = [payload] + ([run_under] if run_under else [])
    if preserve_runfiles_symlinks:
        # A rules_python venv links to its interpreter relative to its final
        # runfiles layout. Dereferencing it in the execroot loses that target.
        tar(
            name = name + "_payload",
            testonly = True,
            srcs = srcs,
            include_runfiles = True,
            mutate = mutate(preserve_symlinks = True, srcs = srcs),
            tags = ["manual"],
        )
    else:
        pkg_tar(
            name = name + "_payload",
            testonly = True,
            srcs = srcs,
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

def arm64_tcg_test(name, payload, tags, image = Label(":guest"), **kwargs):
    """Wraps an ARM64 payload for unprivileged emulation on an AMD64 worker."""

    _guest_test(
        name = name,
        payload = payload,
        image = image,
        emulator = Label("//test/rbe/tcg/qemu:system_aarch64"),
        tags = tags + ["no-local"],
        exec_properties = {
            # Compute units prevent predicted task sizes from overriding CPUs.
            # EstimatedMemory below still controls the memory allocation.
            # https://github.com/buildbuddy-io/buildbuddy/blob/171043b63/enterprise/server/tasksize_model/tasksize_model.go#L190-L198
            "test.EstimatedComputeUnits": "2",
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

def amd64_kvm_test(name, payload, tags, image, run_under = None, **kwargs):
    """Wraps an AMD64 payload for a host with nested KVM, keeping builds remote."""

    # Bazel downloads local spawn inputs before acquiring local test resources.
    # Shared runfiles avoid materializing a separate ext4 image for every queued
    # owner; each running test constructs and removes only its private disk.
    # https://github.com/bazelbuild/bazel/blob/f8278f94e/src/main/java/com/google/devtools/build/lib/exec/local/LocalSpawnRunner.java#L125-L155
    _runtime_payload_image(
        name = name + "_payload_builder",
        testonly = True,
        payload = payload,
        run_under = run_under,
        host_tools = Label(":kvm_host_tools"),
        exec_compatible_with = ["@platforms//os:linux", "@platforms//cpu:x86_64"],
        tags = ["manual"],
    )
    _tcg_test(
        name = name,
        payload = payload,
        image = image,
        machine = "amd64_kvm",
        host_tools = Label(":kvm_host_tools"),
        run_under = run_under,
        payload_builder = ":" + name + "_payload_builder",
        exec_compatible_with = ["@platforms//os:linux", "@platforms//cpu:x86_64"],
        # Compilation remains remote; disk preparation and QEMU run locally.
        tags = tags + ["manual", "no-remote-exec", "no-sandbox", "requires-kvm"],
        **kwargs
    )
