"""Declared inputs and execution requirements for the maintained Docker suites."""

load("@bazel_skylib//lib:shell.bzl", "shell")
load("@rules_foreign_cc//toolchains/native_tools:tool_access.bzl", "access_tool")
load("@rules_oci//oci:defs.bzl", "oci_load")
load("//tools:arch.bzl", "select_arch")
load("//tools:defs.bzl", "go_test")
load("//tools/bazeldefs:platforms.bzl", "docker_test_exec_properties")
load("//tools/bazeldefs:test_architectures.bzl", "test_architecture_tags", "test_architecture_variants", "with_test_architecture")
load(":config.bzl", "AMD64_IMAGES", "AMD64_RUNTIME_IMAGES", "COHORT_IMAGES")

def _image_name(image):
    return image.replace("/", "_").replace("-", "_")

def docker_image_archive(name, image, architecture, source = None):
    """Declares a Docker archive from a source producer or a MODULE pin.

    Args:
      name: Target prefix; the tarball is exposed as name + "_tar".
      image: Image name relative to gvisor.dev/images.
      architecture: Architecture of the declared image.
      source: Optional declared Docker-save archive replacing the registry input.
    """
    if source != None:
        native.filegroup(
            name = name + "_tar",
            testonly = True,
            srcs = [source],
            tags = ["manual"],
        )
        return
    repository = "docker_image_" + _image_name(image) + "_" + architecture
    oci_load(
        name = name,
        image = "@" + repository,
        repo_tags = ["gvisor.dev/images/" + image + ":latest"],
        tags = ["manual"],
    )
    native.filegroup(
        name = name + "_tar",
        srcs = [":" + name],
        output_group = "tarball",
        tags = ["manual"],
    )

def docker_image_archives(name, extra_images = [], source_archives = {}):
    """Declares Docker-format archives for the suite's image inputs.

    Args:
      name: Prefix for the archive targets.
      extra_images: Archives consumed directly without loading them into Docker.
      source_archives: Image names mapped to architecture-to-archive label maps.
    """
    images = {image: True for cohort in COHORT_IMAGES.values() for image in cohort}
    images.update({image: True for image in AMD64_RUNTIME_IMAGES})
    images.update({image: True for image in extra_images})
    for image in sorted(images):
        image_name = _image_name(image)
        architectures = ["amd64"] if image in AMD64_IMAGES else ["amd64", "arm64"]
        for arch in architectures:
            docker_image_archive(
                name = name + "_" + image_name + "_" + arch,
                image = image,
                architecture = arch,
                source = source_archives.get(image, {}).get(arch),
            )

def _daemon_config_impl(ctx):
    runtime = ctx.executable.runtime
    if runtime == None and ctx.attr.runtime_args:
        fail("runtime arguments require a declared runsc binary")
    output = ctx.actions.declare_file(ctx.label.name + ".json")
    ctx.actions.write(output, json.encode({
        "runsc": runtime.short_path if runtime != None else "",
        "native": runtime == None,
        "images": [archive.short_path for archive in ctx.files.images],
        "runtime_args": ctx.attr.runtime_args,
        "ipv6": ctx.attr.ipv6,
    }))
    runfiles = ctx.runfiles(files = ctx.files.images)
    if runtime != None:
        runfiles = runfiles.merge(ctx.runfiles(files = [runtime]))
        runfiles = runfiles.merge(ctx.attr.runtime[DefaultInfo].default_runfiles)
    return [DefaultInfo(files = depset([output]), runfiles = runfiles)]

_docker_daemon_config = rule(
    implementation = _daemon_config_impl,
    doc = "Declares the release, image archives and runtime arguments for an owned Docker daemon.",
    attrs = {
        "runtime": attr.label(executable = True, cfg = "target"),
        "images": attr.label_list(allow_files = True),
        "runtime_args": attr.string_list(),
        "ipv6": attr.bool(doc = "Enable the default Docker bridge's IPv6 subnet."),
    },
)

def docker_daemon_config(name, runtime = Label("//:release"), **kwargs):
    """Declares an owned daemon; runtime=None selects native runc only.

    Args:
      name: Configuration target name.
      runtime: Declared runsc binary, or None for a native image-build daemon.
      **kwargs: Image archives, runtime arguments and other rule attributes.
    """
    _docker_daemon_config(name = name, runtime = runtime, **kwargs)

def docker_test(name, cohort = None, data = [], args = [], owned_args = [], nogo = True, runtime_variants = None, ipv6 = False, **kwargs):
    """Runs an existing Go suite against installed or declared Docker inputs.

    Args:
      name: Existing test target name.
      cohort: Optional key in COHORT_IMAGES identifying owned image inputs.
      data: Other existing runtime inputs.
      args: Arguments shared by the installed and owned test entrypoints.
      owned_args: Additional arguments for owned test entrypoints only.
      nogo: Whether this target owns static analysis of the test sources.
      runtime_variants: Optional named runtime arguments, test_args, data and tags for owned actions.
        An empty name preserves the default owned test instead of an aggregate suite.
      ipv6: Whether the owned daemon provides IPv6 on its default bridge.
      **kwargs: Remaining go_test arguments.
    """
    if cohort == None and (owned_args or runtime_variants != None or ipv6):
        fail("owned arguments, runtime variants and IPv6 require an image cohort")

    # Bazel's native local attribute is nonconfigurable. Keep the installed
    # entrypoint and generate owned variants from the same source/deps.
    # Only the installed target creates a Nogo target: analysis does not need
    # the owned daemon's runtime image archives.
    go_test(
        name = name,
        args = args,
        data = data,
        local = True,
        nogo = nogo,
        **kwargs
    )

    if cohort == None:
        return

    owned_docker_test(
        name = name,
        cohort = cohort,
        data = data,
        args = args + owned_args,
        runtime_variants = runtime_variants,
        ipv6 = ipv6,
        **kwargs
    )

def owned_docker_test(name, cohort = None, data = [], args = [], runtime_variants = None, ipv6 = False, memory = None, free_disk = None, **kwargs):
    """Runs existing Go sources with a declared Docker daemon and images.

    Unlike docker_test, this only declares owned actions; its caller owns the
    installed entrypoint and static analysis of the same sources.

    Args:
      name: Prefix for owned target names.
      cohort: Default COHORT_IMAGES key; individual variants may override it.
      data: Other existing runtime inputs.
      args: Arguments shared by all variants.
      runtime_variants: Optional runtime label, arguments, cohort, test_args, data and tags.
        An empty name preserves the default owned test instead of an aggregate suite.
      ipv6: Whether the owned daemon provides IPv6 on its default bridge.
      memory: Optional test VM memory budget; defaults to the fixture's 4GB.
      free_disk: Optional test VM disk budget; defaults to the existing cohort budget.
      **kwargs: Remaining go_test arguments.
    """
    tests = []
    variants = runtime_variants if runtime_variants != None else [struct(name = "", args = [])]
    for variant in variants:
        variant_cohort = getattr(variant, "cohort", cohort)
        if variant_cohort not in COHORT_IMAGES:
            fail("unknown Docker image cohort: %s" % variant_cohort)
        images = sorted(COHORT_IMAGES[variant_cohort])
        amd64 = images + (AMD64_RUNTIME_IMAGES if variant_cohort == "runtime" else [])
        arm64 = [image for image in images if image not in AMD64_IMAGES]
        prefix = name + ("_" + variant.name if variant.name else "")
        config = prefix + "_docker_config"
        docker_daemon_config(
            name = config,
            testonly = True,
            runtime = getattr(variant, "runtime", Label("//:release")),
            runtime_args = variant.args,
            ipv6 = ipv6,
            images = select_arch(
                amd64 = ["//test/docker:images_" + _image_name(image) + "_amd64_tar" for image in amd64],
                arm64 = ["//test/docker:images_" + _image_name(image) + "_arm64_tar" for image in arm64],
            ),
            tags = ["manual"],
        )
        test = prefix + "_owned"
        owned_kwargs = dict(kwargs)
        owned_kwargs["tags"] = kwargs.get("tags", []) + getattr(variant, "tags", [])
        if hasattr(variant, "test_rule"):
            owned_kwargs["test_rule"] = variant.test_rule
        if arm64 != images:
            owned_kwargs["target_compatible_with"] = kwargs.get("target_compatible_with", []) + select_arch(
                amd64 = [],
                arm64 = ["@platforms//:incompatible"],
            )
        go_test(
            name = test,
            nogo = False,
            args = ["--docker_test_config=$(rootpath :" + config + ")"] + args + getattr(variant, "test_args", []),
            data = data + [":" + config] + getattr(variant, "data", []),
            rundir = ".",
            # Image layers and container writes use the explicitly sized root disk.
            exec_properties = docker_test_exec_properties(
                free_disk = free_disk if free_disk != None else ("30GB" if variant_cohort == "image" else "20GB"),
                memory = memory,
            ),
            **owned_kwargs
        )
        tests.append(test)
    if runtime_variants != None and name + "_owned" not in tests:
        native.test_suite(
            name = name + "_owned",
            tests = tests,
            tags = ["manual"],
            visibility = kwargs.get("visibility"),
        )

def _docker_command_test_impl(ctx):
    command = ctx.executable.command
    wrapper = ctx.executable._wrapper
    config = ctx.file.docker_config
    arguments = [
        "--docker_test_config=" + config.short_path,
        "--",
        command.short_path,
    ] + [ctx.expand_location(arg, targets = ctx.attr.data) for arg in ctx.attr.command_args]
    runner = ctx.actions.declare_file(ctx.label.name + "-runner")
    ctx.actions.write(runner, "\n".join([
        "#!/bin/bash",
        "exec %s %s \"$@\"" % (shell.quote(wrapper.short_path), " ".join([shell.quote(arg) for arg in arguments])),
        "",
    ]), is_executable = True)
    runfiles = ctx.runfiles(files = [command, wrapper, config] + ctx.files.data)
    for target in [ctx.attr.command, ctx.attr._wrapper, ctx.attr.docker_config] + ctx.attr.data:
        dependency_runfiles = target[DefaultInfo].default_runfiles
        if dependency_runfiles:
            runfiles = runfiles.merge(dependency_runfiles)
    return [DefaultInfo(executable = runner, runfiles = runfiles)]

_docker_command_test = rule(
    implementation = _docker_command_test_impl,
    doc = "Runs a declared command with the shared private Docker test daemon.",
    test = True,
    attrs = {
        "command": attr.label(mandatory = True, executable = True, cfg = "target", allow_files = True),
        "command_args": attr.string_list(doc = "Command arguments, with location expansion against data."),
        "data": attr.label_list(allow_files = True),
        "docker_config": attr.label(mandatory = True, allow_single_file = True),
        "_wrapper": attr.label(default = "//test/docker/runner", executable = True, cfg = "target"),
    },
)

docker_command_amd64_test, _docker_command_amd64_transition = with_test_architecture(_docker_command_test, "amd64").build()
docker_command_arm64_test, _docker_command_arm64_transition = with_test_architecture(_docker_command_test, "arm64").build()

def docker_command_test(name, architectures = [], **kwargs):
    """Declares the original command test and requested architecture variants.

    Args:
      name: Existing command test target name.
      architectures: Architectures supported by its declared command and inputs.
      **kwargs: Remaining command test attributes.
    """
    kwargs["tags"] = test_architecture_tags(architectures, kwargs.get("tags", []))
    _docker_command_test(name = name, **kwargs)
    test_architecture_variants(
        name,
        architectures,
        {"amd64": docker_command_amd64_test, "arm64": docker_command_arm64_test},
        kwargs,
    )

def _image_source_command_impl(ctx):
    make = access_tool(Label("@rules_foreign_cc//toolchains:make_toolchain"), ctx)
    if make.target == None or make.env != {"MAKE": make.path}:
        fail("image-source checks require the declared GNU Make toolchain")
    crane = ctx.toolchains[Label("@rules_oci//oci:crane_toolchain_type")].crane_info.binary
    tool_root = ctx.label.name + "_tools"
    tools = {tool_root + "/" + file.path: file for file in make.target.files.to_list()}
    tools[tool_root + "/crane"] = crane
    command = ctx.actions.declare_file(ctx.label.name)
    ctx.actions.write(
        command,
        "\n".join([
            "#!/bin/bash",
            "set -euo pipefail",
            'exec /bin/bash %s test %s "${TEST_SRCDIR:?}/%s/%s" "${TEST_SRCDIR}/%s/crane" %s %s %s' % (
                shell.quote(ctx.file._script.short_path),
                shell.quote(ctx.attr.architecture),
                tool_root,
                make.path,
                tool_root,
                shell.quote(ctx.file._makefile.short_path),
                shell.quote(ctx.file._contexts.short_path),
                shell.quote(ctx.attr.image_class),
            ),
            "",
        ]),
        is_executable = True,
    )
    runfiles = ctx.runfiles(
        files = [ctx.file._contexts, ctx.file._makefile, ctx.file._script],
        root_symlinks = tools,
    )
    make_runfiles = make.target[DefaultInfo].default_runfiles
    if make_runfiles:
        runfiles = runfiles.merge(make_runfiles)
    return DefaultInfo(executable = command, runfiles = runfiles)

image_source_command = rule(
    implementation = _image_source_command_impl,
    doc = "Runs a canonical image-source check with declared Make and crane.",
    executable = True,
    attrs = {
        "architecture": attr.string(mandatory = True, values = ["x86_64", "aarch64"]),
        "image_class": attr.string(default = "cpu", values = ["cpu", "gpu"], doc = "Selects Make's CPU or GPU/ML test-image cohort."),
        "_makefile": attr.label(default = "//tools:images.mk", allow_single_file = True),
        "_contexts": attr.label(default = "//images:source_contexts", allow_single_file = True),
        "_script": attr.label(default = "//test/docker:source_images.sh", allow_single_file = True),
    },
    toolchains = [
        "@rules_foreign_cc//toolchains:make_toolchain",
        "@rules_oci//oci:crane_toolchain_type",
    ],
)

def _source_image_archive_impl(ctx):
    make = access_tool(Label("@rules_foreign_cc//toolchains:make_toolchain"), ctx)
    if make.target == None or make.env != {"MAKE": make.path}:
        fail("source image archives require the declared GNU Make toolchain")
    crane = ctx.toolchains[Label("@rules_oci//oci:crane_toolchain_type")].crane_info.binary
    archive = ctx.actions.declare_file(ctx.label.name + ".tar")
    inputs = depset(
        [ctx.file._contexts, ctx.file._makefile, ctx.file._script, ctx.file._config, crane],
        transitive = [make.target.files, make.target[DefaultInfo].default_runfiles.files],
    )
    ctx.actions.run(
        executable = ctx.attr._wrapper[DefaultInfo].files_to_run,
        arguments = [
            "--runtime=runc",
            "--docker_test_config=" + ctx.file._config.path,
            "--",
            "/bin/bash",
            ctx.file._script.path,
            "archive",
            ctx.attr.architecture,
            make.path,
            crane.path,
            ctx.file._makefile.path,
            ctx.file._contexts.path,
            ctx.attr.image,
            archive.path,
        ],
        inputs = inputs,
        outputs = [archive],
        # The pinned execution image supplies the daemon and Docker CLI.
        env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
        mnemonic = "DockerSourceImage",
        progress_message = "Constructing source image %s (%s)" % (ctx.attr.image, ctx.attr.architecture),
        # Dockerfile base tags and package mirrors are mutable network inputs.
        # Share this action among consumers, but do not reuse remote/disk results.
        execution_requirements = {"no-cache": "1"},
    )
    return [DefaultInfo(files = depset([archive]))]

source_image_archive = rule(
    implementation = _source_image_archive_impl,
    doc = "Constructs one Docker-save archive with the existing native daemon and Make image rules.",
    attrs = {
        "architecture": attr.string(mandatory = True, values = ["x86_64", "aarch64"]),
        "image": attr.string(mandatory = True, doc = "Image name relative to gvisor.dev/images."),
        "_config": attr.label(default = "//test/docker:image_source_config", allow_single_file = True),
        "_contexts": attr.label(default = "//images:source_contexts", allow_single_file = True),
        "_makefile": attr.label(default = "//tools:images.mk", allow_single_file = True),
        "_script": attr.label(default = "//test/docker:source_images.sh", allow_single_file = True),
        "_wrapper": attr.label(default = "//test/docker/runner", executable = True, cfg = "exec"),
    },
    toolchains = [
        "@rules_foreign_cc//toolchains:make_toolchain",
        "@rules_oci//oci:crane_toolchain_type",
    ],
)
